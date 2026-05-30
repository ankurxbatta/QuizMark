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
import base64
import uuid
from datetime import datetime
from collections import defaultdict

from app.tasks.celery_app import celery_app
from app.core.database import AsyncSessionLocal
from app.models.models import Question, IngestJob, IngestJobStatus
from app.services.pdf_service import parse_pdf_into_chunks
from app.services.question_generator import generate_questions_from_chunks
from app.services.llm_service import llm_service

_ALLOWED_QTYPES = {"mcq", "true_false", "short_answer"}
_ALLOWED_DIFFICULTIES = {"easy", "medium", "hard"}


@celery_app.task(bind=True, max_retries=0, time_limit=3600)
def ingest_pdf_task(
    self,
    job_id: str,
    pdf_b64: str,
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
        pdf_bytes = base64.b64decode(pdf_b64)
        loop.run_until_complete(
            _run_ingest(job_id, pdf_bytes, question_type, count_per_chapter)
        )
    except Exception as exc:
        loop.run_until_complete(_mark_failed(job_id, str(exc)))
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

        now = datetime.utcnow()
        job.status = IngestJobStatus.processing
        job.started_at = now
        job.completed_at = None
        job.error_message = None
        job.chapters_done = 0
        job.questions_created = 0
        job.total_chapters = 0
        job.current_chapter = None
        job.current_chapter_title = None
        job.progress_message = "Parsing PDF into teaching chunks."
        job.last_heartbeat_at = now
        await db.commit()

        # ── Step 1: Parse PDF into structured chunks ─────────────────────────
        chunks = parse_pdf_into_chunks(pdf_bytes, max_pages=620)
        if not chunks:
            job.status = IngestJobStatus.failed
            job.error_message = "No usable text chunks extracted from PDF."
            job.progress_message = "Parsing finished with no usable chunks."
            job.completed_at = datetime.utcnow()
            job.last_heartbeat_at = datetime.utcnow()
            await db.commit()
            return

        # ── Step 1b: Embed chunks and store in MongoDB for vector search ────────
        if getattr(__import__("app.core.config", fromlist=["settings"]).settings, "MONGODB_ENABLED", False):
            try:
                from app.services.mongo_vector_store import store_chunk as _mongo_store
                mongo_errors = 0
                for _chunk in chunks:
                    try:
                        _emb = await llm_service.embed(_chunk.text[:1500])
                        await _mongo_store(_chunk, _emb, job_id)
                    except Exception:
                        mongo_errors += 1
                if mongo_errors:
                    job.progress_message = (
                        f"Parsed {len(chunks)} chunks. "
                        f"MongoDB: {mongo_errors} storage errors (non-fatal)."
                    )
                    job.last_heartbeat_at = datetime.utcnow()
                    await db.commit()
            except Exception as _exc:
                job.progress_message = f"MongoDB storage skipped: {str(_exc)[:120]}"
                job.last_heartbeat_at = datetime.utcnow()
                await db.commit()

        # ── Step 1c: Describe vector-graphic pages via GPT-4o Vision ────────────
        try:
            from app.services.pdf_extractor import describe_graph_chunks, EnhancedChunk
            has_graph_chunks = any(
                getattr(c, "graph_page_nums", None) for c in chunks
            )
            if has_graph_chunks:
                job.progress_message = "Describing charts and graphs with GPT-4o Vision…"
                job.last_heartbeat_at = datetime.utcnow()
                await db.commit()
                await describe_graph_chunks(chunks, pdf_bytes)
        except Exception as _exc:
            job.progress_message = f"Graph vision skipped: {str(_exc)[:120]}"
            job.last_heartbeat_at = datetime.utcnow()
            await db.commit()

        # ── Step 2: Group by chapter ─────────────────────────────────────────
        # chapters_map: chapter_num → list of chunks
        chapters_map: dict[int, list] = defaultdict(list)
        for chunk in chunks:
            chapters_map[chunk.chapter_num].append(chunk)

        # Sort chapters in order (include chapter 0 for fallback parses)
        chapter_nums = sorted(chapters_map.keys())
        job.total_chapters = len(chapter_nums)
        job.progress_message = (
            f"Parsed {len(chunks)} chunks across {len(chapter_nums)} chapters. "
            "Starting chapter generation."
        )
        job.last_heartbeat_at = datetime.utcnow()
        await db.commit()

        total_created = 0
        chapter_failures: list[str] = []

        # ── Step 3: Process each chapter ─────────────────────────────────────
        for idx, ch_num in enumerate(chapter_nums, start=1):
            ch_chunks = chapters_map[ch_num]
            if not ch_chunks:
                job.chapters_done = idx
                job.progress_message = f"Skipped chapter {ch_num} because no chunks were available."
                job.last_heartbeat_at = datetime.utcnow()
                await db.commit()
                continue

            chapter_title = ch_chunks[0].chapter_title
            topic_tag = ch_chunks[0].topic_tag
            chapter_label = f"Chapter {ch_num}: {chapter_title}"

            job.current_chapter = ch_num
            job.current_chapter_title = chapter_title
            job.progress_message = (
                f"Processing {chapter_label} ({idx}/{len(chapter_nums)})."
            )
            job.last_heartbeat_at = datetime.utcnow()
            await db.commit()

            try:
                questions_data = await generate_questions_from_chunks(
                    ch_chunks,
                    question_type=question_type,
                    count=count_per_chapter,
                )
            except Exception as exc:
                message = f"{chapter_label} generation failed: {str(exc)[:180]}"
                chapter_failures.append(message)
                job.chapters_done = idx
                job.progress_message = message
                job.last_heartbeat_at = datetime.utcnow()
                await db.commit()
                continue

            # ── Step 4: Embed and persist ─────────────────────────────────────
            chapter_created = 0
            for q_data in questions_data:
                q_text = q_data.get("question_text", "").strip()
                m_answer = q_data.get("model_answer", "").strip()
                if not q_text or not m_answer:
                    continue

                qtype = q_data.get("question_type", question_type)
                if qtype not in _ALLOWED_QTYPES:
                    qtype = question_type

                difficulty = q_data.get("difficulty", "medium")
                if difficulty not in _ALLOWED_DIFFICULTIES:
                    difficulty = "medium"

                try:
                    max_marks = float(q_data.get("max_marks", 5))
                except (TypeError, ValueError):
                    max_marks = 5.0

                q = Question(
                    question_text=q_text,
                    question_type=qtype,
                    model_answer=m_answer,
                    rubric=q_data.get("rubric", ""),
                    max_marks=max_marks,
                    topic_tag=q_data.get("topic_tag", topic_tag),
                    difficulty=difficulty,
                    source_page_range=q_data.get("_page_range", ""),
                    source_chunk=q_data.get("_source_chunk", ""),
                )
                try:
                    q.embedding = await llm_service.embed(f"{q_text} {m_answer}")
                except Exception:
                    q.embedding = None

                db.add(q)
                chapter_created += 1

            try:
                await db.commit()
            except Exception as exc:
                await db.rollback()
                message = f"{chapter_label} persistence failed: {str(exc)[:180]}"
                chapter_failures.append(message)
                job.chapters_done = idx
                job.progress_message = message
                job.last_heartbeat_at = datetime.utcnow()
                await db.commit()
                continue

            total_created += chapter_created

            # ── Step 5: Update job progress ───────────────────────────────────
            job.chapters_done = idx
            job.questions_created = total_created
            job.progress_message = (
                f"Finished {chapter_label}. Added {chapter_created} questions. "
                f"Total created: {total_created}."
            )
            job.last_heartbeat_at = datetime.utcnow()
            await db.commit()

        # ── Done ─────────────────────────────────────────────────────────────
        job.current_chapter = None
        job.current_chapter_title = None
        job.completed_at = datetime.utcnow()
        job.questions_created = total_created
        job.last_heartbeat_at = datetime.utcnow()
        if total_created == 0:
            job.status = IngestJobStatus.failed
            if chapter_failures:
                job.error_message = "No questions generated. " + " | ".join(chapter_failures[:3])
            else:
                job.error_message = "No valid questions were produced from extracted content."
            job.progress_message = job.error_message
        else:
            job.status = IngestJobStatus.done
            if chapter_failures:
                job.error_message = (
                    f"Completed with {len(chapter_failures)} chapter failures. "
                    f"{' | '.join(chapter_failures[:2])}"
                )
                job.progress_message = (
                    f"Completed with partial failures. Created {total_created} questions."
                )
            else:
                job.error_message = None
                job.progress_message = f"Completed successfully. Created {total_created} questions."
        await db.commit()


async def _mark_failed(job_id: str, error: str):
    async with AsyncSessionLocal() as db:
        job = await db.get(IngestJob, uuid.UUID(job_id))
        if job:
            job.status = IngestJobStatus.failed
            job.error_message = error[:1000]
            job.progress_message = f"Ingest failed: {error[:500]}"
            job.completed_at = datetime.utcnow()
            job.last_heartbeat_at = datetime.utcnow()
            await db.commit()
