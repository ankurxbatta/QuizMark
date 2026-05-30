"""
ingest_tasks.py  —  Celery background task for full-textbook PDF ingestion.

Processes a PDF chapter-by-chapter:
  1. parse_pdf_into_chunks() — deep structural parsing
  2. Store chunks in MongoDB with embeddings (RAG source)
  3. Run describe_graph_chunks() via GPT-4o Vision
  4. Group chunks by chapter, generate questions per chapter
  5. Persist each question to MongoDB questions collection
  6. Update IngestJob progress in real time
"""
import asyncio
import base64
import uuid
from datetime import datetime, timezone
from collections import defaultdict

from app.tasks.celery_app import celery_app
from app.core.database import get_mongo_db
from app.models.models import IngestJobStatus
from app.services.pdf_service import parse_pdf_into_chunks
from app.services.question_generator import generate_questions_from_chunks
from app.services.llm_service import llm_service

_ALLOWED_QTYPES = {"mcq", "true_false", "short_answer"}
_ALLOWED_DIFFICULTIES = {"easy", "medium", "hard"}


@celery_app.task(bind=True, max_retries=0, time_limit=3600)
def ingest_book_only_task(self, job_id: str, pdf_b64: str, book_id: str):
    """Parse PDF into chunks + embed + store in Library. No question generation."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pdf_bytes = base64.b64decode(pdf_b64)
        loop.run_until_complete(_run_ingest_only(job_id, pdf_bytes, book_id))
    except Exception as exc:
        loop.run_until_complete(_mark_failed(job_id, str(exc)))
    finally:
        loop.close()


async def _run_ingest_only(job_id: str, pdf_bytes: bytes, book_id: str):
    """Steps 1-3 of ingest: parse → embed → store chunks. Skips question generation."""
    db = get_mongo_db()
    now = datetime.now(timezone.utc)
    await db["ingest_jobs"].update_one(
        {"_id": job_id},
        {"$set": {
            "status": IngestJobStatus.processing.value,
            "started_at": now, "completed_at": None, "error_message": None,
            "chapters_done": 0, "questions_created": 0, "total_chapters": 0,
            "current_chapter": None, "current_chapter_title": None,
            "progress_message": "Parsing PDF into teaching chunks…",
            "last_heartbeat_at": now,
        }},
    )

    chunks = parse_pdf_into_chunks(pdf_bytes, max_pages=620)
    if not chunks:
        await _update_job(db, job_id,
            status=IngestJobStatus.failed.value,
            error_message="No usable chunks extracted.",
            progress_message="Parsing returned no usable content.",
            completed_at=datetime.now(timezone.utc),
        )
        return

    chapters_map: dict[int, list] = defaultdict(list)
    for c in chunks:
        chapters_map[c.chapter_num].append(c)
    chapter_nums = sorted(chapters_map.keys())
    total_chapters = len(chapter_nums)

    await _update_job(db, job_id,
        total_chapters=total_chapters,
        progress_message=f"Parsed {len(chunks)} chunks across {total_chapters} chapters. Storing…",
    )

    # Embed + store chunks
    from app.services.mongo_vector_store import store_chunk as _mongo_store
    errors = 0
    for i, chunk in enumerate(chunks):
        try:
            emb = await llm_service.embed(chunk.text[:1500])
            await _mongo_store(chunk, emb, book_id)
        except Exception:
            errors += 1
        if i % 50 == 0:
            await _update_job(db, job_id,
                progress_message=f"Stored {i+1}/{len(chunks)} chunks…",
            )

    # Gemini Vision for chart pages
    try:
        from app.services.pdf_extractor import describe_graph_chunks
        if any(getattr(c, "graph_page_nums", None) for c in chunks):
            await _update_job(db, job_id, progress_message="Describing charts with Gemini Vision…")
            await describe_graph_chunks(chunks, pdf_bytes)
    except Exception:
        pass

    await _update_job(db, job_id,
        status=IngestJobStatus.done.value,
        total_chapters=total_chapters,
        chapters_done=total_chapters,
        completed_at=datetime.now(timezone.utc),
        error_message=f"{errors} embed errors (non-fatal)." if errors else None,
        progress_message=f"Book stored in Library: {len(chunks)} chunks, {total_chapters} chapters.",
    )


@celery_app.task(bind=True, max_retries=0, time_limit=3600)
def ingest_pdf_task(self, job_id: str, pdf_b64: str, question_type: str, count_per_chapter: int):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pdf_bytes = base64.b64decode(pdf_b64)
        loop.run_until_complete(_run_ingest(job_id, pdf_bytes, question_type, count_per_chapter))
    except Exception as exc:
        loop.run_until_complete(_mark_failed(job_id, str(exc)))
    finally:
        loop.close()


async def _update_job(db, job_id: str, **fields):
    fields["last_heartbeat_at"] = datetime.now(timezone.utc)
    await db["ingest_jobs"].update_one({"_id": job_id}, {"$set": fields})


async def _run_ingest(job_id: str, pdf_bytes: bytes, question_type: str, count_per_chapter: int):
    db = get_mongo_db()

    now = datetime.now(timezone.utc)
    await db["ingest_jobs"].update_one(
        {"_id": job_id},
        {"$set": {
            "status": IngestJobStatus.processing.value,
            "started_at": now,
            "completed_at": None,
            "error_message": None,
            "chapters_done": 0,
            "questions_created": 0,
            "total_chapters": 0,
            "current_chapter": None,
            "current_chapter_title": None,
            "progress_message": "Parsing PDF into teaching chunks.",
            "last_heartbeat_at": now,
        }},
    )

    # ── Step 1: Parse PDF ─────────────────────────────────────────────────────
    chunks = parse_pdf_into_chunks(pdf_bytes, max_pages=620)
    if not chunks:
        await _update_job(db, job_id,
            status=IngestJobStatus.failed.value,
            error_message="No usable text chunks extracted from PDF.",
            progress_message="Parsing finished with no usable chunks.",
            completed_at=datetime.now(timezone.utc),
        )
        return

    # ── Step 2: Embed chunks → MongoDB vector store ───────────────────────────
    mongo_errors = 0
    try:
        from app.services.mongo_vector_store import store_chunk as _mongo_store
        for _chunk in chunks:
            try:
                _emb = await llm_service.embed(_chunk.text[:1500])
                await _mongo_store(_chunk, _emb, job_id)
            except Exception:
                mongo_errors += 1
        if mongo_errors:
            await _update_job(db, job_id,
                progress_message=f"Parsed {len(chunks)} chunks. MongoDB: {mongo_errors} storage errors (non-fatal)."
            )
    except Exception as _exc:
        await _update_job(db, job_id,
            progress_message=f"MongoDB chunk storage skipped: {str(_exc)[:120]}"
        )

    # ── Step 3: GPT-4o Vision descriptions for chart pages ───────────────────
    try:
        from app.services.pdf_extractor import describe_graph_chunks
        has_graph_chunks = any(getattr(c, "graph_page_nums", None) for c in chunks)
        if has_graph_chunks:
            await _update_job(db, job_id, progress_message="Describing charts with GPT-4o Vision…")
            await describe_graph_chunks(chunks, pdf_bytes)
    except Exception as _exc:
        await _update_job(db, job_id,
            progress_message=f"Graph vision skipped: {str(_exc)[:120]}"
        )

    # ── Step 4: Group by chapter ──────────────────────────────────────────────
    chapters_map: dict[int, list] = defaultdict(list)
    for chunk in chunks:
        chapters_map[chunk.chapter_num].append(chunk)

    chapter_nums = sorted(chapters_map.keys())
    await _update_job(db, job_id,
        total_chapters=len(chapter_nums),
        progress_message=f"Parsed {len(chunks)} chunks across {len(chapter_nums)} chapters. Starting generation.",
    )

    total_created = 0
    chapter_failures: list[str] = []

    # ── Step 5: Generate questions per chapter ────────────────────────────────
    for idx, ch_num in enumerate(chapter_nums, start=1):
        ch_chunks = chapters_map[ch_num]
        if not ch_chunks:
            await _update_job(db, job_id,
                chapters_done=idx,
                progress_message=f"Skipped chapter {ch_num} (no chunks).",
            )
            continue

        chapter_title = ch_chunks[0].chapter_title
        topic_tag = ch_chunks[0].topic_tag
        chapter_label = f"Chapter {ch_num}: {chapter_title}"

        await _update_job(db, job_id,
            current_chapter=ch_num,
            current_chapter_title=chapter_title,
            progress_message=f"Processing {chapter_label} ({idx}/{len(chapter_nums)}).",
        )

        try:
            questions_data = await generate_questions_from_chunks(
                ch_chunks, question_type=question_type, count=count_per_chapter,
            )
        except Exception as exc:
            msg = f"{chapter_label} generation failed: {str(exc)[:180]}"
            chapter_failures.append(msg)
            await _update_job(db, job_id, chapters_done=idx, progress_message=msg)
            continue

        # ── Step 6: Embed + persist questions ─────────────────────────────────
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

            embedding = None
            try:
                embedding = await llm_service.embed(f"{q_text} {m_answer}")
            except Exception:
                pass

            q_doc = {
                "_id": str(uuid.uuid4()),
                "question_text": q_text,
                "question_type": qtype,
                "model_answer": m_answer,
                "rubric": q_data.get("rubric", ""),
                "max_marks": max_marks,
                "topic_tag": q_data.get("topic_tag", topic_tag),
                "difficulty": difficulty,
                "source_page_range": q_data.get("_page_range", ""),
                "source_chunk": q_data.get("_source_chunk", ""),
                "embedding": embedding,
                "assigned_student_ids": [],
                "created_at": datetime.now(timezone.utc),
            }
            try:
                await db["questions"].insert_one(q_doc)
                chapter_created += 1
            except Exception as exc:
                msg = f"{chapter_label} question persistence failed: {str(exc)[:180]}"
                chapter_failures.append(msg)

        total_created += chapter_created
        await _update_job(db, job_id,
            chapters_done=idx,
            questions_created=total_created,
            progress_message=f"Finished {chapter_label}. Added {chapter_created} questions. Total: {total_created}.",
        )

    # ── Done ──────────────────────────────────────────────────────────────────
    if total_created == 0:
        error_msg = "No questions generated."
        if chapter_failures:
            error_msg += " " + " | ".join(chapter_failures[:3])
        await _update_job(db, job_id,
            status=IngestJobStatus.failed.value,
            current_chapter=None,
            current_chapter_title=None,
            completed_at=datetime.now(timezone.utc),
            error_message=error_msg,
            progress_message=error_msg,
        )
    else:
        prog = f"Completed successfully. Created {total_created} questions."
        err = None
        if chapter_failures:
            err = f"Completed with {len(chapter_failures)} chapter failures. " + " | ".join(chapter_failures[:2])
            prog = f"Completed with partial failures. Created {total_created} questions."
        await _update_job(db, job_id,
            status=IngestJobStatus.done.value,
            current_chapter=None,
            current_chapter_title=None,
            completed_at=datetime.now(timezone.utc),
            questions_created=total_created,
            error_message=err,
            progress_message=prog,
        )


@celery_app.task(bind=True, max_retries=0, time_limit=3600)
def generate_from_book_task(self, job_id: str, book_id: str, question_type: str, count_per_chapter: int, chapter_nums: list | None = None, difficulty: str = "all"):
    """Generate questions from chunks already stored in MongoDB (no PDF re-upload needed)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_generate_from_book(job_id, book_id, question_type, count_per_chapter, chapter_nums, difficulty))
    except Exception as exc:
        loop.run_until_complete(_mark_failed(job_id, str(exc)))
    finally:
        loop.close()


async def _run_generate_from_book(job_id: str, book_id: str, question_type: str, count_per_chapter: int, chapter_nums: list | None = None, difficulty: str = "all"):
    db = get_mongo_db()
    now = datetime.now(timezone.utc)
    await db["ingest_jobs"].update_one(
        {"_id": job_id},
        {"$set": {
            "status": IngestJobStatus.processing.value,
            "started_at": now,
            "progress_message": f"Fetching chunks for '{book_id}' from database…",
            "last_heartbeat_at": now,
        }},
    )

    # Load all chunks from MongoDB for this book
    raw_docs = await db["pdf_chunks"].find({"book_id": book_id}).to_list(length=10000)
    if not raw_docs:
        await _update_job(db, job_id,
            status=IngestJobStatus.failed.value,
            error_message=f"No chunks found for book_id '{book_id}'.",
            progress_message="Failed — book not found in database.",
            completed_at=datetime.now(timezone.utc),
        )
        return

    # Reconstruct lightweight chunk objects compatible with generate_questions_from_chunks
    class _DbChunk:
        def __init__(self, doc):
            self.chapter_num = doc.get("chapter_num", 0)
            self.chapter_title = doc.get("chapter_title", "Unknown")
            self.section_title = doc.get("section_title", "")
            self.topic_tag = doc.get("topic_tag", "")
            self.text = doc.get("text", "")
            self.page_start = doc.get("page_start", 0)
            self.page_end = doc.get("page_end", 0)
            self.has_formula = doc.get("has_formula", False)
            self.has_example = doc.get("has_example", False)
            self.teaching_density = doc.get("teaching_density", 0.0)
            self.key_terms = doc.get("key_terms", [])
            self.image_texts = doc.get("image_texts", [])
            self.table_texts = doc.get("table_texts", [])
            self.math_text = doc.get("math_text", "")

        @property
        def label(self):
            return f"Ch{self.chapter_num} § {self.section_title}"

        def to_prompt_block(self):
            parts = [
                f"[SOURCE: {self.label} | Topic: {self.topic_tag} | "
                f"Pages {self.page_start}–{self.page_end}]",
            ]
            if self.has_formula:
                parts.append("[Contains: mathematical formulas]")
            if self.has_example:
                parts.append("[Contains: worked examples]")
            parts.append("")
            parts.append(self.text)
            return "\n".join(parts)

    chunks = [_DbChunk(doc) for doc in raw_docs]

    # Group by chapter, filtering to requested chapters if provided
    chapters_map: dict[int, list] = defaultdict(list)
    for chunk in chunks:
        chapters_map[chunk.chapter_num].append(chunk)

    # chapter_nums param comes from the task args (not the local var) — use a different name
    selected_chapter_nums = sorted(
        (n for n in chapters_map.keys() if not chapter_nums or n in chapter_nums)
    )

    diff_label = f" [{difficulty}]" if difficulty != "all" else ""
    await _update_job(db, job_id,
        total_chapters=len(selected_chapter_nums),
        progress_message=f"Loaded {len(chunks)} chunks. Generating from {len(selected_chapter_nums)} chapters{diff_label}.",
    )

    total_created = 0
    chapter_failures: list[str] = []

    for idx, ch_num in enumerate(selected_chapter_nums, start=1):
        ch_chunks = chapters_map[ch_num]
        if not ch_chunks:
            await _update_job(db, job_id, chapters_done=idx, progress_message=f"Skipped chapter {ch_num}.")
            continue

        chapter_title = ch_chunks[0].chapter_title
        topic_tag = ch_chunks[0].topic_tag
        chapter_label = f"Chapter {ch_num}: {chapter_title}"

        await _update_job(db, job_id,
            current_chapter=ch_num,
            current_chapter_title=chapter_title,
            progress_message=f"Generating from {chapter_label} ({idx}/{len(selected_chapter_nums)}).",
        )

        try:
            questions_data = await generate_questions_from_chunks(
                ch_chunks, question_type=question_type, count=count_per_chapter, difficulty=difficulty,
            )
        except Exception as exc:
            msg = f"{chapter_label} generation failed: {str(exc)[:180]}"
            chapter_failures.append(msg)
            await _update_job(db, job_id, chapters_done=idx, progress_message=msg)
            continue

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

            embedding = None
            try:
                embedding = await llm_service.embed(f"{q_text} {m_answer}")
            except Exception:
                pass

            q_doc = {
                "_id": str(uuid.uuid4()),
                "question_text": q_text,
                "question_type": qtype,
                "model_answer": m_answer,
                "rubric": q_data.get("rubric", ""),
                "max_marks": max_marks,
                "topic_tag": q_data.get("topic_tag", topic_tag),
                "difficulty": difficulty,
                "source_page_range": q_data.get("_page_range", ""),
                "source_chunk": q_data.get("_source_chunk", ""),
                "embedding": embedding,
                "assigned_student_ids": [],
                "created_at": datetime.now(timezone.utc),
            }
            try:
                await db["questions"].insert_one(q_doc)
                chapter_created += 1
            except Exception as exc:
                chapter_failures.append(f"{chapter_label}: {str(exc)[:120]}")

        total_created += chapter_created
        await _update_job(db, job_id,
            chapters_done=idx,
            questions_created=total_created,
            progress_message=f"Done {chapter_label}. +{chapter_created} questions. Total: {total_created}.",
        )

    if total_created == 0:
        error_msg = "No questions generated."
        if chapter_failures:
            error_msg += " " + " | ".join(chapter_failures[:3])
        await _update_job(db, job_id,
            status=IngestJobStatus.failed.value,
            current_chapter=None, current_chapter_title=None,
            completed_at=datetime.now(timezone.utc),
            error_message=error_msg, progress_message=error_msg,
        )
    else:
        prog = f"Completed. Created {total_created} questions from database."
        err = None
        if chapter_failures:
            err = f"Completed with {len(chapter_failures)} chapter failures."
            prog = f"Completed with partial failures. Created {total_created} questions."
        await _update_job(db, job_id,
            status=IngestJobStatus.done.value,
            current_chapter=None, current_chapter_title=None,
            completed_at=datetime.now(timezone.utc),
            questions_created=total_created,
            error_message=err, progress_message=prog,
        )


async def _mark_failed(job_id: str, error: str):
    db = get_mongo_db()
    await db["ingest_jobs"].update_one(
        {"_id": job_id},
        {"$set": {
            "status": IngestJobStatus.failed.value,
            "error_message": error[:1000],
            "progress_message": f"Ingest failed: {error[:500]}",
            "completed_at": datetime.now(timezone.utc),
            "last_heartbeat_at": datetime.now(timezone.utc),
        }},
    )
