"""
ingest_tasks.py  —  Celery background task for full-textbook PDF ingestion.

Processes a PDF chapter-by-chapter:
  1. parse_pdf_into_chunks() — deep structural parsing
  2. Group chunks by chapter
  3. For each chapter group, run generate_questions_from_chunks()
  4. Embed each question and persist to DB
  5. Update IngestJob progress in real time

This runs entirely in the Celery worker so the API stays responsive
even while processing a 600-page textbook.
"""
import asyncio
import uuid
from datetime import datetime
from collections import defaultdict

from app.tasks.celery_app import celery_app
from app.core.database import AsyncSessionLocal
from app.models.models import Question, IngestJob, IngestJobStatus
from app.services.pdf_service import parse_pdf_into_chunks
from app.services.question_generator import generate_questions_from_chunks
from app.services.llm_service import llm_service


@celery_app.task(bind=True, max_retries=2, time_limit=3600)
def ingest_pdf_task(
    self,
    job_id: str,
    pdf_bytes: bytes,
    question_type: str,
    count_per_chapter: int,
):
    """
    Full-textbook background ingest.
    Updates IngestJob.status / chapters_done / questions_created as it goes.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            _run_ingest(job_id, pdf_bytes, question_type, count_per_chapter)
        )
    except Exception as exc:
        loop.run_until_complete(_mark_failed(job_id, str(exc)))
        raise self.retry(exc=exc, countdown=30)
    finally:
        loop.close()


async def _run_ingest(
    job_id: str,
    pdf_bytes: bytes,
    question_type: str,
    count_per_chapter: int,
):
    async with AsyncSessionLocal() as db:
        job: IngestJob = await db.get(IngestJob, uuid.UUID(job_id))
        if not job:
            return

        job.status = IngestJobStatus.processing
        job.started_at = datetime.utcnow()
        await db.commit()

        # ── Step 1: Parse PDF into structured chunks ─────────────────────────
        chunks = parse_pdf_into_chunks(pdf_bytes, max_pages=620)
        if not chunks:
            job.status = IngestJobStatus.failed
            job.error_message = "No usable text chunks extracted from PDF."
            await db.commit()
            return

        # ── Step 2: Group by chapter ─────────────────────────────────────────
        # chapters_map: chapter_num → list of chunks
        chapters_map: dict[int, list] = defaultdict(list)
        for chunk in chunks:
            chapters_map[chunk.chapter_num].append(chunk)

        # Sort chapters in order (skip chapter 0 = preface/boilerplate)
        chapter_nums = sorted(k for k in chapters_map.keys() if k > 0)

        total_created = 0

        # ── Step 3: Process each chapter ─────────────────────────────────────
        for ch_num in chapter_nums:
            ch_chunks = chapters_map[ch_num]
            if not ch_chunks:
                continue

            chapter_title = ch_chunks[0].chapter_title
            topic_tag = ch_chunks[0].topic_tag

            try:
                questions_data = await generate_questions_from_chunks(
                    ch_chunks,
                    question_type=question_type,
                    count=count_per_chapter,
                )
            except Exception as e:
                # Log but continue with next chapter
                continue

            # ── Step 4: Embed and persist ─────────────────────────────────────
            for q_data in questions_data:
                q_text = q_data.get("question_text", "").strip()
                m_answer = q_data.get("model_answer", "").strip()
                if not q_text or not m_answer:
                    continue

                q = Question(
                    question_text=q_text,
                    question_type=q_data.get("question_type", question_type),
                    model_answer=m_answer,
                    rubric=q_data.get("rubric", ""),
                    max_marks=float(q_data.get("max_marks", 5)),
                    topic_tag=q_data.get("topic_tag", topic_tag),
                    difficulty=q_data.get("difficulty", "medium"),
                    source_page_range=q_data.get("_page_range", ""),
                    source_chunk=q_data.get("_source_chunk", ""),
                )
                try:
                    q.embedding = await llm_service.embed(f"{q_text} {m_answer}")
                except Exception:
                    q.embedding = None

                db.add(q)
                total_created += 1

            await db.commit()

            # ── Step 5: Update job progress ───────────────────────────────────
            job.chapters_done += 1
            job.questions_created = total_created
            await db.commit()

        # ── Done ─────────────────────────────────────────────────────────────
        job.status = IngestJobStatus.done
        job.completed_at = datetime.utcnow()
        job.questions_created = total_created
        await db.commit()


async def _mark_failed(job_id: str, error: str):
    async with AsyncSessionLocal() as db:
        job = await db.get(IngestJob, uuid.UUID(job_id))
        if job:
            job.status = IngestJobStatus.failed
            job.error_message = error[:1000]
            await db.commit()
