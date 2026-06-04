"""
ingest_tasks.py  —  Celery background tasks for PDF ingestion + question gen.

Resumable ingestion (preferred path for /ingest-book):
  • Page-by-page processing via ChunkAccumulator, windowed at INGEST_PAGE_WINDOW.
  • After each window: vision-describe new chunks → batch-embed → bulk-insert →
    save checkpoint (next_page, accumulator state, pages_done, chunks_stored).
  • Time-budget guard re-.delay()s the same task before the Celery soft limit,
    so big books auto-continue across worker restarts. Re-uploading the same PDF
    (same content hash) picks up from the last checkpointed page.

Generation (preferred path for /generate/from-book):
  • Parallel chapters with bounded concurrency (GEN_CHAPTER_CONCURRENCY).
  • Cross-chapter embedding-based near-duplicate dedup before insert.
  • Batched question embeddings (one HTTP round-trip per chapter's questions).
"""
import asyncio
import base64
import logging
import time
import uuid
from datetime import datetime, timezone
from collections import defaultdict

from celery.exceptions import SoftTimeLimitExceeded

from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.core.database import get_mongo_db
from app.models.models import IngestJobStatus
from app.services.pdf_service import parse_pdf_into_chunks
from app.services.question_generator import generate_questions_from_chunks, DbChunk
from app.services.question_orchestrator import orchestrate_question_bank
from app.services.llm_service import llm_service
from app.services.text_cleaner import clean_chunk_doc

logger = logging.getLogger(__name__)

_ALLOWED_QTYPES = {"mcq", "true_false", "short_answer"}
_ALLOWED_DIFFICULTIES = {"easy", "medium", "hard"}


def _chunk_embedding_text(chunk) -> str:
    parts = [
        f"{chunk.chapter_title} {chunk.section_title}",
        chunk.text[:1500],
    ]
    for label, values in (
        ("Tables", getattr(chunk, "table_texts", [])),
        ("Images and charts", getattr(chunk, "image_texts", [])),
    ):
        if values:
            parts.append(f"{label}:\n" + "\n".join(values)[:1200])
    math_text = getattr(chunk, "math_text", "")
    if math_text:
        parts.append(f"Formula snippets:\n{math_text[:800]}")
    return "\n\n".join(part for part in parts if part)


async def _embed_and_store_sequential(chunks, book_id, db, job_id):
    """
    Embed chunks sequentially and store them in MongoDB.
    Avoids batch processing to prevent API exhaustion and 429 errors.
    Returns (stored_count, error_count).
    """
    from app.services.mongo_vector_store import store_chunk as _mongo_store

    if not chunks:
        return 0, 0

    await _update_job(db, job_id,
        progress_message=f"Embedding and storing {len(chunks)} chunks sequentially…",
    )

    stored = 0
    errors = 0
    for i, chunk in enumerate(chunks):
        text = _chunk_embedding_text(chunk)
        try:
            emb = await llm_service.embed(text)
            if emb:
                await _mongo_store(chunk, emb, book_id)
                stored += 1
            else:
                errors += 1
        except Exception as exc:
            logger.error(f"Sequential embedding failed for chunk {i}: {exc}")
            errors += 1

        # Update the UI every 10 chunks so it never gets stuck
        if i % 10 == 0 and i > 0:
            await _update_job(db, job_id,
                progress_message=f"Stored {i}/{len(chunks)} chunks…",
            )
            
        # Small delay to prevent rate limiting
        await asyncio.sleep(settings.GEMINI_EMBEDDING_DELAY_SECONDS)

    return stored, errors


@celery_app.task(bind=True, queue="ingest_tasks", max_retries=0, soft_time_limit=1800, time_limit=2100)
def ingest_book_resumable_task(self, job_id: str, pdf_b64: str, book_id: str, book_hash: str):
    """
    Page-by-page resumable PDF ingestion. Auto-continues across the Celery time
    limit by re-.delay()ing the same task with the same (job_id, book_hash).
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pdf_bytes = base64.b64decode(pdf_b64)
        should_continue = loop.run_until_complete(
            _run_ingest_resumable(job_id, pdf_bytes, book_id, book_hash)
        )
        if should_continue:
            # Re-queue self to keep going under a fresh time budget
            ingest_book_resumable_task.delay(job_id, pdf_b64, book_id, book_hash)
    except SoftTimeLimitExceeded:
        # Checkpoint is already saved at last window boundary — requeue and exit
        try:
            ingest_book_resumable_task.delay(job_id, pdf_b64, book_id, book_hash)
        except Exception:
            loop.run_until_complete(_mark_failed(job_id, "Soft time limit hit and re-queue failed."))
    except Exception as exc:
        loop.run_until_complete(_mark_failed(job_id, str(exc)))
    finally:
        loop.close()


# Backward-compatible alias — older callers / imports still work
ingest_book_only_task = ingest_book_resumable_task


async def _run_ingest_resumable(job_id: str, pdf_bytes: bytes, book_id: str, book_hash: str) -> bool:
    """
    Process page windows until either the PDF is fully read OR the per-task time
    budget elapses. Returns True if the caller should re-queue (more pages left).
    """
    import fitz  # PyMuPDF — pulled here so the task module imports cheaply
    from app.services.pdf_extractor import (
        ChunkAccumulator, process_page_window, _OCR_AVAILABLE,
        describe_graph_chunks, transcribe_math_chunks,
    )
    from app.services.mongo_vector_store import (
        get_checkpoint, save_checkpoint, store_chunks_bulk,
    )

    db = get_mongo_db()
    started_wall = time.monotonic()

    # ── Restore or create checkpoint ──────────────────────────────────────────
    ck = await get_checkpoint(book_hash) or {}
    next_page = int(ck.get("next_page", 0))
    pages_done = int(ck.get("pages_done", 0))
    chunks_stored = int(ck.get("chunks_stored", 0))
    ocr_active = bool(ck.get("ocr_active", _OCR_AVAILABLE))
    accumulator = ChunkAccumulator(
        min_chunk_chars=settings.PDF_MIN_CHUNK_CHARS,
        max_chunk_chars=settings.PDF_MAX_CHUNK_CHARS,
        state=ck.get("state"),
    )
    pages_at_start = pages_done

    # ── Open PDF + size it ────────────────────────────────────────────────────
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = min(doc.page_count, settings.PDF_MAX_PAGES)

    now = datetime.now(timezone.utc)
    starting_msg = (
        f"Resuming from page {next_page + 1}/{total_pages}"
        if next_page > 0
        else f"Reading page {next_page + 1}/{total_pages}"
    )
    await _update_job(db, job_id,
        status=IngestJobStatus.processing.value,
        started_at=ck.get("created_at") or now,
        completed_at=None, error_message=None,
        total_pages=total_pages,
        pages_done=pages_done,
        progress_percent=int(100 * pages_done / max(total_pages, 1)),
        progress_message=starting_msg,
    )

    if next_page >= total_pages:
        # Already fully read but checkpoint not marked complete — finalize now
        trailing = accumulator.finalize(total_pages)
        chunks_stored += await _process_window_chunks(
            trailing, pdf_bytes, book_id, book_hash, db, job_id
        )
        await save_checkpoint(book_hash, {
            "book_id": book_id, "job_id": job_id,
            "total_pages": total_pages,
            "next_page": total_pages, "pages_done": total_pages,
            "chunks_stored": chunks_stored, "ocr_active": ocr_active,
            "status": "complete",
            "state": accumulator.serialize(),
        })
        await _update_job(db, job_id,
            status=IngestJobStatus.done.value,
            pages_done=total_pages, progress_percent=100,
            completed_at=datetime.now(timezone.utc),
            progress_message=f"Book stored in Library: {chunks_stored} chunks, {total_pages} pages.",
        )
        doc.close()
        return False

    # ── Window loop ───────────────────────────────────────────────────────────
    window = max(1, int(settings.INGEST_PAGE_WINDOW))
    budget = max(60, int(settings.INGEST_TIME_BUDGET_SECONDS))

    while next_page < total_pages:
        end = min(next_page + window, total_pages)
        window_chunks, ocr_active = process_page_window(
            doc, next_page, end, accumulator, ocr_active
        )
        # Vision + embed + bulk-insert this window's flushed chunks
        added = await _process_window_chunks(
            window_chunks, pdf_bytes, book_id, book_hash, db, job_id,
        )
        chunks_stored += added

        next_page = end
        pages_done = end

        await save_checkpoint(book_hash, {
            "book_id": book_id, "job_id": job_id,
            "total_pages": total_pages,
            "next_page": next_page, "pages_done": pages_done,
            "chunks_stored": chunks_stored, "ocr_active": ocr_active,
            "status": "in_progress",
            "state": accumulator.serialize(),
        })

        pct = int(100 * pages_done / max(total_pages, 1))
        await _update_job(db, job_id,
            pages_done=pages_done, total_pages=total_pages,
            progress_percent=pct,
            progress_message=f"Read {pages_done}/{total_pages} pages · {chunks_stored} chunks stored",
        )

        # Time budget check — re-queue if we'd risk hitting the soft limit
        if time.monotonic() - started_wall > budget and pages_done < total_pages:
            # Poison-pill guard — only requeue if we made forward progress this run
            if pages_done <= pages_at_start:
                doc.close()
                await _mark_failed(job_id, "Ingestion stalled — no forward progress in this run.")
                return False
            doc.close()
            await _update_job(db, job_id,
                progress_message=f"Read {pages_done}/{total_pages} pages — continuing in next worker slot…",
            )
            return True  # caller will re-.delay()

    # ── Final flush of trailing buffer ────────────────────────────────────────
    trailing = accumulator.finalize(total_pages)
    chunks_stored += await _process_window_chunks(
        trailing, pdf_bytes, book_id, book_hash, db, job_id,
    )
    await save_checkpoint(book_hash, {
        "book_id": book_id, "job_id": job_id,
        "total_pages": total_pages,
        "next_page": total_pages, "pages_done": total_pages,
        "chunks_stored": chunks_stored, "ocr_active": ocr_active,
        "status": "complete",
        "state": accumulator.serialize(),
    })
    await _update_job(db, job_id,
        status=IngestJobStatus.done.value,
        pages_done=total_pages, total_pages=total_pages,
        progress_percent=100,
        completed_at=datetime.now(timezone.utc),
        progress_message=f"Book stored in Library: {chunks_stored} chunks, {total_pages} pages.",
    )
    doc.close()
    return False


async def _process_window_chunks(
    chunks: list,
    pdf_bytes: bytes,
    book_id: str,
    book_hash: str,
    db,
    job_id: str,
) -> int:
    """Vision-describe (if enabled) → batch-embed → bulk-insert one window's chunks."""
    if not chunks:
        return 0

    if settings.ENABLE_VISION_EXTRACTION:
        try:
            from app.services.pdf_extractor import describe_graph_chunks, transcribe_math_chunks
            if any(getattr(c, "figure_rects", None) for c in chunks):
                await describe_graph_chunks(chunks, pdf_bytes, job_id=job_id)
            if any(getattr(c, "math_rects", None) for c in chunks):
                await transcribe_math_chunks(chunks, pdf_bytes, job_id=job_id)
        except Exception as exc:
            logger.warning(f"Vision pass on window failed (non-fatal): {exc}")

    # Clean PDF noise from each chunk before embedding
    for c in chunks:
        try:
            cleaned = clean_chunk_doc({
                "text": getattr(c, "text", ""),
                "math_text": getattr(c, "math_text", ""),
                "image_texts": getattr(c, "image_texts", []),
                "table_texts": getattr(c, "table_texts", []),
                "key_terms": getattr(c, "key_terms", []),
            })
            c.text = cleaned["text"]
            c.math_text = cleaned.get("math_text", "")
            c.image_texts = cleaned.get("image_texts", [])
            c.table_texts = cleaned.get("table_texts", [])
            c.key_terms = cleaned.get("key_terms", [])
        except Exception:
            pass  # never let cleaner break ingest

    # Batch embed
    texts = [_chunk_embedding_text(c) for c in chunks]
    embeddings: list[list[float]] = []
    try:
        embeddings = await llm_service.embed_batch(texts)
    except Exception as exc:
        logger.warning(f"embed_batch failed; per-chunk fallback: {exc}")
    if len(embeddings) != len(chunks):
        # Top up via per-chunk so chunks/embeddings stay aligned
        embeddings = []
        for t in texts:
            try:
                embeddings.append(await llm_service.embed(t))
            except Exception:
                embeddings.append([])

    from app.services.mongo_vector_store import store_chunks_bulk
    return await store_chunks_bulk(chunks, embeddings, book_id, book_hash)


@celery_app.task(bind=True, queue="ingest_tasks", max_retries=0, soft_time_limit=1800, time_limit=2100)
def ingest_pdf_task(self, job_id: str, pdf_b64: str, question_type: str, count_per_chapter: int):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pdf_bytes = base64.b64decode(pdf_b64)
        loop.run_until_complete(_run_ingest(job_id, pdf_bytes, question_type, count_per_chapter))
    except SoftTimeLimitExceeded:
        loop.run_until_complete(_mark_failed(job_id, "Task timed out after 30 minutes. The PDF may be too large or the API is slow."))
    except Exception as exc:
        loop.run_until_complete(_mark_failed(job_id, str(exc)))
    finally:
        loop.close()


async def _update_job(db, job_id: str, **fields):
    fields["last_heartbeat_at"] = datetime.now(timezone.utc)
    await db["ingest_jobs"].update_one({"_id": job_id}, {"$set": fields})


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _normalise_q(q_data: dict, question_type: str, topic_tag: str) -> dict | None:
    """Apply allow-listed defaults to a raw generated question dict."""
    q_text = q_data.get("question_text", "").strip()
    m_answer = q_data.get("model_answer", "").strip()
    if not q_text or not m_answer:
        return None
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
    return {
        "question_text": q_text,
        "question_type": qtype,
        "model_answer": m_answer,
        "rubric": q_data.get("rubric", ""),
        "max_marks": max_marks,
        "topic_tag": q_data.get("topic_tag", topic_tag),
        "difficulty": difficulty,
        "bloom_level": q_data.get("bloom_level", "L3"),
        "_page_range": q_data.get("_page_range", ""),
        "_source_chunk": q_data.get("_source_chunk", ""),
    }


async def _generate_chapter(
    *,
    chapter_num: int,
    chapter_title: str,
    topic_tag: str,
    book_id: str,
    question_type: str,
    count_per_chapter: int,
    difficulty: str,
    existing_questions: list[str],
) -> tuple[list[dict], list[list[float]]]:
    """
    Run one chapter's orchestration → normalise → batch-embed.
    Returns (validated question dicts, parallel list of embeddings).
    Caller is responsible for cross-chapter dedup + DB insertion.
    """
    try:
        raw = await orchestrate_question_bank(
            chapter_topic=f"{chapter_title} {topic_tag}",
            book_id=book_id,
            question_type=question_type,
            count=count_per_chapter,
            difficulty=difficulty,
            existing_questions=existing_questions,
        )
    except Exception as exc:
        logger.warning(f"orchestrate Chapter {chapter_num} failed: {exc}")
        return [], []

    normalised: list[dict] = []
    for q_data in raw:
        norm = _normalise_q(q_data, question_type, topic_tag)
        if norm is not None:
            normalised.append(norm)
    if not normalised:
        return [], []

    texts = [f"{q['question_text']} {q['model_answer']}" for q in normalised]
    try:
        embeddings = await llm_service.embed_batch(texts)
    except Exception as exc:
        logger.warning(f"embed_batch for chapter {chapter_num} failed: {exc}")
        embeddings = [[] for _ in normalised]
    # Top up any missing embeddings
    if len(embeddings) != len(normalised):
        embeddings = [[] for _ in normalised]
    for i, emb in enumerate(embeddings):
        if not emb:
            try:
                embeddings[i] = await llm_service.embed(texts[i])
            except Exception:
                embeddings[i] = []
    return normalised, embeddings


def _dedup_across_chapters(
    questions: list[dict],
    embeddings: list[list[float]],
    threshold: float,
) -> tuple[list[dict], list[list[float]]]:
    """Drop questions whose embedding is too similar to one already kept."""
    kept_q: list[dict] = []
    kept_e: list[list[float]] = []
    for q, e in zip(questions, embeddings):
        if e and any(_cosine(e, ke) >= threshold for ke in kept_e if ke):
            continue
        kept_q.append(q)
        kept_e.append(e)
    return kept_q, kept_e


async def _run_chapters_parallel(
    *,
    db,
    job_id: str,
    chapters_map: dict | None,   # dict[ch_num, list[chunks]] — used to discover titles; may be None
    chapter_nums: list,           # iterable of ch_num
    book_id: str,
    question_type: str,
    count_per_chapter: int,
    difficulty: str,
    existing_q_texts: list[str],
    chapter_failures: list[str],
    chapter_meta: dict | None = None,  # {ch_num: {"chapter_title", "topic_tag"}} for from-book path
) -> int:
    """
    Generate questions across many chapters concurrently, then cross-dedup by
    embedding cosine, then bulk-insert. Returns total questions inserted.
    """
    sem = asyncio.Semaphore(max(1, int(settings.GEN_CHAPTER_CONCURRENCY)))

    async def _bounded(ch_num: int, chapter_title: str, topic_tag: str):
        async with sem:
            return await _generate_chapter(
                chapter_num=ch_num,
                chapter_title=chapter_title,
                topic_tag=topic_tag,
                book_id=book_id,
                question_type=question_type,
                count_per_chapter=count_per_chapter,
                difficulty=difficulty,
                existing_questions=list(existing_q_texts),  # snapshot per chapter
            )

    tasks = []
    chapter_labels: list[str] = []
    for ch_num in chapter_nums:
        if chapter_meta is not None and ch_num in chapter_meta:
            chapter_title = chapter_meta[ch_num].get("chapter_title", f"Chapter {ch_num}")
            topic_tag = chapter_meta[ch_num].get("topic_tag", chapter_title)
        else:
            ch_chunks = (chapters_map or {}).get(ch_num) or []
            if not ch_chunks:
                chapter_failures.append(f"Chapter {ch_num}: no chunks")
                continue
            chapter_title = ch_chunks[0].chapter_title
            topic_tag = ch_chunks[0].topic_tag
        chapter_labels.append(f"Chapter {ch_num}: {chapter_title}")
        tasks.append(_bounded(ch_num, chapter_title, topic_tag))

    await _update_job(db, job_id,
        progress_message=f"Generating across {len(tasks)} chapters in parallel…",
    )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Flatten + dedup across all chapters
    all_q: list[dict] = []
    all_e: list[list[float]] = []
    for i, res in enumerate(results):
        label = chapter_labels[i] if i < len(chapter_labels) else f"job {i}"
        if isinstance(res, Exception):
            chapter_failures.append(f"{label}: {str(res)[:160]}")
            continue
        questions, embeddings = res
        all_q.extend(questions)
        all_e.extend(embeddings)

    kept_q, kept_e = _dedup_across_chapters(
        all_q, all_e, threshold=float(settings.DEDUP_SIMILARITY_THRESHOLD)
    )

    # Bulk-insert
    inserted = 0
    if kept_q:
        docs = []
        for q, e in zip(kept_q, kept_e):
            docs.append({
                "_id": str(uuid.uuid4()),
                "question_text": q["question_text"],
                "question_type": q["question_type"],
                "model_answer": q["model_answer"],
                "rubric": q["rubric"],
                "max_marks": q["max_marks"],
                "topic_tag": q["topic_tag"],
                "difficulty": q["difficulty"],
                "bloom_level": q.get("bloom_level", "L3"),
                "source_page_range": q.get("_page_range", ""),
                "source_chunk": q.get("_source_chunk", ""),
                "embedding": e or None,
                "assigned_student_ids": [],
                "created_at": datetime.now(timezone.utc),
            })
        try:
            result = await db["questions"].insert_many(docs, ordered=False)
            inserted = len(result.inserted_ids)
        except Exception as exc:
            chapter_failures.append(f"Bulk insert: {str(exc)[:160]}")

    await _update_job(db, job_id,
        chapters_done=len(tasks),
        questions_created=inserted,
        progress_message=f"Generated {inserted} questions across {len(tasks)} chapters (after dedup).",
    )
    return inserted


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
    chunks = parse_pdf_into_chunks(pdf_bytes, max_pages=settings.PDF_MAX_PAGES)
    if not chunks:
        await _update_job(db, job_id,
            status=IngestJobStatus.failed.value,
            error_message="No usable text chunks extracted from PDF.",
            progress_message="Parsing finished with no usable chunks.",
            completed_at=datetime.now(timezone.utc),
        )
        return

    # ── Step 2: Gemini Vision descriptions for chart pages (optional) ─────────
    if settings.ENABLE_VISION_EXTRACTION:
        try:
            from app.services.pdf_extractor import describe_graph_chunks
            has_graph_chunks = any(getattr(c, "graph_page_nums", None) for c in chunks)
            if has_graph_chunks:
                await _update_job(db, job_id, progress_message="Describing charts with Gemini Vision…")
                await describe_graph_chunks(chunks, pdf_bytes, job_id=job_id)
        except Exception as _exc:
            await _update_job(db, job_id,
                progress_message=f"Graph vision skipped: {str(_exc)[:120]}"
            )
    else:
        logger.info("Vision extraction disabled. Skipping chart descriptions.")

    # ── Step 3: Embed chunks → MongoDB vector store (sequential) ──────────────
    stored, mongo_errors = await _embed_and_store_sequential(chunks, book_id, db, job_id)
    if mongo_errors:
        await _update_job(db, job_id,
            progress_message=f"Parsed {len(chunks)} chunks. MongoDB: {mongo_errors} storage errors (non-fatal)."
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

    # Seed uniqueness context from existing questions in the DB
    existing_q_docs = await db["questions"].find({}, {"question_text": 1}).to_list(length=500)
    existing_q_texts: list[str] = [d.get("question_text", "") for d in existing_q_docs if d.get("question_text")]

    # ── Step 5: Generate questions per chapter (parallel) ─────────────────────
    total_created = await _run_chapters_parallel(
        db=db, job_id=job_id,
        chapters_map=chapters_map, chapter_nums=chapter_nums,
        book_id=job_id,
        question_type=question_type,
        count_per_chapter=count_per_chapter,
        difficulty="all",
        existing_q_texts=existing_q_texts,
        chapter_failures=chapter_failures,
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


@celery_app.task(bind=True, queue="gen_tasks", max_retries=0, soft_time_limit=1800, time_limit=2100)
def generate_from_book_task(self, job_id: str, book_id: str, question_type: str, count_per_chapter: int, chapter_nums: list | None = None, difficulty: str = "all"):
    """Generate questions from chunks already stored in MongoDB (no PDF re-upload needed)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_generate_from_book(job_id, book_id, question_type, count_per_chapter, chapter_nums, difficulty))
    except SoftTimeLimitExceeded:
        loop.run_until_complete(_mark_failed(job_id, "Task timed out after 30 minutes."))
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

    # Discover chapters via a lightweight distinct query (no full chunk load)
    pipeline = [
        {"$match": {"book_id": book_id}},
        {"$group": {
            "_id": "$chapter_num",
            "chapter_title": {"$first": "$chapter_title"},
            "topic_tag": {"$first": "$topic_tag"},
        }},
        {"$sort": {"_id": 1}},
    ]
    chapter_meta = await db["pdf_chunks"].aggregate(pipeline).to_list(length=200)
    if not chapter_meta:
        await _update_job(db, job_id,
            status=IngestJobStatus.failed.value,
            error_message=f"No chunks found for book_id '{book_id}'.",
            progress_message="Failed — book not found in database.",
            completed_at=datetime.now(timezone.utc),
        )
        return

    # Filter to requested chapter numbers if provided
    selected_chapters = [
        m for m in chapter_meta
        if not chapter_nums or m["_id"] in chapter_nums
    ]

    diff_label = f" [{difficulty}]" if difficulty != "all" else ""
    await _update_job(db, job_id,
        total_chapters=len(selected_chapters),
        progress_message=f"Found {len(selected_chapters)} chapters in '{book_id}'. Using DeepSearch retrieval{diff_label}.",
    )

    # Seed uniqueness context from existing questions in the DB
    existing_q_docs = await db["questions"].find({}, {"question_text": 1}).to_list(length=500)
    existing_q_texts: list[str] = [d.get("question_text", "") for d in existing_q_docs if d.get("question_text")]

    chapter_failures: list[str] = []
    chapter_nums_ordered = [m["_id"] for m in selected_chapters]
    chapter_meta_map = {
        m["_id"]: {"chapter_title": m.get("chapter_title", f"Chapter {m['_id']}"),
                   "topic_tag": m.get("topic_tag", m.get("chapter_title", ""))}
        for m in selected_chapters
    }
    total_created = await _run_chapters_parallel(
        db=db, job_id=job_id,
        chapters_map=None,
        chapter_nums=chapter_nums_ordered,
        book_id=book_id,
        question_type=question_type,
        count_per_chapter=count_per_chapter,
        difficulty=difficulty,
        existing_q_texts=existing_q_texts,
        chapter_failures=chapter_failures,
        chapter_meta=chapter_meta_map,
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
