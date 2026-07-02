import asyncio
import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional
import base64

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Header
from motor.motor_asyncio import AsyncIOMotorDatabase

from jose import JWTError

from app.core.database import get_db
from app.core.security import require_instructor, get_current_user, decode_token
from app.core.config import settings
from app.models.models import IngestJobStatus
from app.schemas.schemas import (
    AssessmentQuestionOut,
    QuestionAssigneeOut,
    QuestionAssigneeUpdate,
    QuestionCreate,
    QuestionGenerateResponse,
    QuestionOut,
    QuestionUpdate,
)
from app.services.question_generator import generate_questions, generate_questions_from_chunks
from app.services.llm_service import llm_service
from app.services.pdf_service import parse_pdf_into_chunks, get_pdf_info, extract_chapters_from_pdf
from app.tasks.ingest_tasks import ingest_pdf_task, generate_from_book_task, ingest_book_only_task

router = APIRouter()

logger = logging.getLogger(__name__)


async def _read_upload_within_limit(file: UploadFile) -> bytes:
    """Read an upload while enforcing UPLOAD_MAX_SIZE_MB *before* buffering the
    entire body.

    Rejects early on a declared size (Content-Length / ``file.size``) when
    available, then streams the body in bounded 1 MiB chunks and aborts with a
    413 the moment the cumulative size crosses the limit — so an oversized (or
    lying) upload never gets fully materialised in memory.
    """
    max_bytes = settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024
    declared = getattr(file, "size", None)
    if declared is not None and declared > max_bytes:
        raise HTTPException(413, f"File exceeds {settings.UPLOAD_MAX_SIZE_MB} MB limit.")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, f"File exceeds {settings.UPLOAD_MAX_SIZE_MB} MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def _q_out(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = doc.pop("_id")
    doc.setdefault("assigned_student_ids", [])
    doc.setdefault("source_page_range", None)
    doc.setdefault("source_chunk", None)
    doc.setdefault("assets", [])
    doc.pop("embedding", None)
    return doc


# ── Chapter extraction ─────────────────────────────────────────────────────────

@router.post("/chapters")
async def extract_chapters(
    file: UploadFile = File(...),
    _: dict = Depends(require_instructor),
):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(415, "Only PDF files are supported.")
    raw_bytes = await _read_upload_within_limit(file)
    chapters = await asyncio.to_thread(
        extract_chapters_from_pdf, raw_bytes, max_pages=settings.PDF_MAX_PAGES
    )
    return {"chapters": chapters}


# ── Async full-book ingest ─────────────────────────────────────────────────────

@router.post("/generate/async")
async def generate_async(
    file: UploadFile = File(...),
    question_type: str = Query("short_answer", enum=["mcq", "true_false", "short_answer"]),
    count_per_chapter: int = Query(10, ge=1, le=50),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    filename = (file.filename or "").lower()
    if not filename.endswith(".pdf"):
        raise HTTPException(415, "Async ingestion only supports PDF files.")
    raw_bytes = await _read_upload_within_limit(file)

    info = await asyncio.to_thread(get_pdf_info, raw_bytes)
    now = datetime.now(timezone.utc)
    job_id = str(uuid.uuid4())

    job_doc = {
        "_id": job_id,
        "filename": file.filename or "upload.pdf",
        "total_pages": info.get("pages", 0),
        "question_type": question_type,
        "count_per_chapter": count_per_chapter,
        "status": IngestJobStatus.queued.value,
        "chapters_done": 0,
        "questions_created": 0,
        "total_chapters": 0,
        "current_chapter": None,
        "current_chapter_title": None,
        "progress_message": "Queued for worker pickup.",
        "last_heartbeat_at": now,
        "error_message": None,
        "started_at": None,
        "completed_at": None,
        "created_at": now,
    }
    await db["ingest_jobs"].insert_one(job_doc)

    pdf_b64 = base64.b64encode(raw_bytes).decode("utf-8")
    ingest_pdf_task.delay(job_id, pdf_b64, question_type, count_per_chapter)

    return {
        "job_id": job_id,
        "filename": file.filename,
        "total_pages": info.get("pages"),
        "status": "queued",
        "total_chapters": 0,
        "chapters_done": 0,
        "current_chapter": None,
        "current_chapter_title": None,
        "questions_created": 0,
        "progress_message": "Queued for worker pickup.",
        "last_heartbeat_at": now.isoformat(),
        "error": None,
        "created_at": now.isoformat(),
        "started_at": None,
        "completed_at": None,
        "message": "Processing started. Poll /questions/jobs/{job_id} for progress.",
    }


# ── Library cache — MUST be before /books/{book_id} so FastAPI doesn't match "cache" as a book_id
@router.get("/books/cache")
async def list_cached_books_early(
    _: dict = Depends(require_instructor),
):
    """List incomplete ingestion checkpoints (the 'cached / resumable' tab)."""
    from app.services.mongo_vector_store import list_incomplete_checkpoints
    items = await list_incomplete_checkpoints(limit=100)
    out = []
    for ck in items:
        out.append({
            "book_hash": ck.get("_id"),
            "book_id": ck.get("book_id", ""),
            "filename": ck.get("filename", ""),
            "job_id": ck.get("job_id", ""),
            "total_pages": ck.get("total_pages", 0),
            "pages_done": ck.get("pages_done", 0),
            "chunks_stored": ck.get("chunks_stored", 0),
            "progress_percent": int(100 * ck.get("pages_done", 0) / max(ck.get("total_pages") or 1, 1)),
            "status": ck.get("status", "in_progress"),
            "updated_at": ck.get("updated_at").isoformat() if ck.get("updated_at") else None,
        })
    return {"cached": out}


# ── Single book detail ────────────────────────────────────────────────────────

@router.get("/books/{book_id}")
async def get_book(
    book_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    """Return stats and chapter list for a single book."""
    pipeline = [
        {"$match": {"book_id": book_id}},
        {"$group": {
            "_id": "$book_id",
            "total_chunks": {"$sum": 1},
            "chapter_set": {"$addToSet": {"num": "$chapter_num", "title": "$chapter_title"}},
            "with_tables": {"$sum": {"$cond": ["$has_tables", 1, 0]}},
            "with_math":   {"$sum": {"$cond": ["$has_math",   1, 0]}},
            "with_images": {"$sum": {"$cond": ["$has_images", 1, 0]}},
            "ingested_at": {"$max": "$created_at"},
        }},
    ]
    raw = await db["pdf_chunks"].aggregate(pipeline).to_list(length=1)
    if not raw:
        raise HTTPException(404, f"Book '{book_id}' not found in Library.")

    doc = raw[0]
    ch_by_num: dict[int, str] = {}
    for c in doc.get("chapter_set", []):
        num, title = c.get("num"), c.get("title", "")
        if not title or num is None:
            continue
        title = re.sub(r"(\s*\.\s*){2,}$", "", title).strip()
        if num not in ch_by_num or len(title) < len(ch_by_num[num]):
            ch_by_num[num] = title
    chapters = sorted(ch_by_num.items(), key=lambda x: x[0])

    job = await db["ingest_jobs"].find_one({"_id": book_id}, projection={"filename": 1})
    raw_name = (job.get("filename", "") if job else "") or book_id
    # Split CamelCase and hyphens/underscores → readable title
    display_name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw_name).replace("-", " ").replace("_", " ")
    display_name = re.sub(r"\s+", " ", display_name).strip().title()

    return {
        "book_id": book_id,
        "display_name": display_name,
        "total_chunks": doc["total_chunks"],
        "total_chapters": len(chapters),
        "chapters": [{"num": n, "title": t} for n, t in chapters[:30]],
        "with_tables": doc["with_tables"],
        "with_math": doc["with_math"],
        "with_images": doc["with_images"],
        "ingested_at": doc["ingested_at"].isoformat() if doc.get("ingested_at") else None,
    }


# ── Add book to Library (chunks only, no question generation) ─────────────────

@router.post("/ingest-book")
async def ingest_book_to_library(
    file: UploadFile = File(...),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    """
    Upload a PDF for resumable, page-by-page ingestion into the Library.
    Re-uploading the same content resumes from the last checkpointed page;
    if the book is already fully ingested, this is a no-op.
    """
    from app.services.mongo_vector_store import get_checkpoint, save_checkpoint, save_book_pdf

    filename = (file.filename or "upload.pdf")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(415, "Only PDF files are supported.")
    raw_bytes = await _read_upload_within_limit(file)

    book_hash = hashlib.sha256(raw_bytes).hexdigest()[:16]
    info = await asyncio.to_thread(get_pdf_info, raw_bytes)
    if info.get("error") or not info.get("pages"):
        # Reject unreadable PDFs here instead of queueing a job doomed to fail
        raise HTTPException(
            422,
            f"Could not read this PDF ({info.get('error') or 'no pages found'}). "
            "The file may be corrupt, encrypted, or not a real PDF.",
        )
    # Derive book_id from file CONTENT (not the bare filename) so two different
    # PDFs sharing a name can't collide across the ~9 book-scoped collections,
    # while re-uploading identical content stays stable/idempotent. Prefix with a
    # slug of the filename for human readability. (book_hash is a sha256 of the
    # raw bytes.) NOTE: rows written before this change used the filename-stem
    # scheme and are not migrated.
    _slug = re.sub(r"[^a-z0-9]+", "-", filename.rsplit(".", 1)[0].lower()).strip("-")[:40]
    book_id = f"{_slug}-{book_hash}" if _slug else book_hash
    now = datetime.now(timezone.utc)

    checkpoint = await get_checkpoint(book_hash)

    # Fallback: checkpoint missing but partial chunks for this hash still exist.
    # Could happen if Mongo dropped the checkpoint, an earlier worker crashed
    # before its first window completed, or someone deleted the checkpoint
    # without clearing chunks. Reconstruct a stub so the user gets resume.
    if not checkpoint:
        last_chunk = await db["pdf_chunks"].find(
            {"book_hash": book_hash}, {"page_end": 1}
        ).sort("page_end", -1).limit(1).to_list(length=1)
        if last_chunk:
            resumed_page = int(last_chunk[0].get("page_end", 0))
            await save_checkpoint(book_hash, {
                "book_id": book_id, "filename": filename,
                "job_id": str(uuid.uuid4()),
                "total_pages": info.get("pages", 0),
                "next_page": resumed_page, "pages_done": resumed_page,
                "chunks_stored": await db["pdf_chunks"].count_documents({"book_hash": book_hash}),
                "ocr_active": True,
                "status": "in_progress",
                "state": None,  # buffer state lost — accept the small gap, better than restarting
            })
            checkpoint = await get_checkpoint(book_hash)

    if checkpoint and checkpoint.get("status") == "complete":
        # Already fully ingested — return the existing job for visibility
        existing_job_id = checkpoint.get("job_id")
        job = await db["ingest_jobs"].find_one({"_id": existing_job_id}) if existing_job_id else None
        return {
            "job_id": existing_job_id or "",
            "filename": filename,
            "book_id": checkpoint.get("book_id", book_id),
            "book_hash": book_hash,
            "total_pages": checkpoint.get("total_pages", info.get("pages")),
            "pages_done": checkpoint.get("total_pages", 0),
            "progress_percent": 100,
            "status": (job or {}).get("status", "done"),
            "already_ingested": True,
            "resumed": False,
            "resumed_from_page": None,
            "total_chapters": (job or {}).get("total_chapters", 0),
            "chapters_done": (job or {}).get("chapters_done", 0),
            "current_chapter": None,
            "current_chapter_title": None,
            "questions_created": (job or {}).get("questions_created", 0),
            "progress_message": "Book is already fully ingested. Clear cache to re-ingest.",
            "last_heartbeat_at": now.isoformat(),
            "error": None,
            "created_at": now.isoformat(),
            "started_at": None,
            "completed_at": (job or {}).get("completed_at").isoformat() if job and job.get("completed_at") else None,
        }

    resumed = bool(checkpoint and checkpoint.get("status") == "in_progress")
    job_id = (checkpoint.get("job_id") if resumed else None) or str(uuid.uuid4())
    pages_done = int((checkpoint or {}).get("pages_done", 0))

    # Guard: if this book's job is actively running (fresh heartbeat), don't
    # queue a second concurrent task — two workers racing on the same checkpoint
    # corrupt each other's progress. Just return the live job status instead.
    if resumed:
        job = await db["ingest_jobs"].find_one({"_id": job_id})
        if job and job.get("status") in ("queued", "processing"):
            hb = job.get("last_heartbeat_at")
            if hb is not None and hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            if hb and (now - hb).total_seconds() < 600:
                return {
                    "job_id": job_id,
                    "filename": filename,
                    "book_id": checkpoint.get("book_id", book_id),
                    "book_hash": book_hash,
                    "total_pages": checkpoint.get("total_pages", info.get("pages")),
                    "pages_done": pages_done,
                    "progress_percent": int(100 * pages_done / max(checkpoint.get("total_pages") or 1, 1)),
                    "status": job.get("status"),
                    "already_ingested": False,
                    "already_running": True,
                    "resumed": True,
                    "resumed_from_page": pages_done,
                    "total_chapters": 0,
                    "chapters_done": 0,
                    "current_chapter": None,
                    "current_chapter_title": None,
                    "questions_created": 0,
                    "progress_message": job.get("progress_message")
                        or "Ingestion already in progress for this book.",
                    "last_heartbeat_at": hb.isoformat() if hb else None,
                    "error": None,
                    "created_at": now.isoformat(),
                    "started_at": None,
                    "completed_at": None,
                }

    job_doc = {
        "_id": job_id,
        "filename": filename,
        "book_id": book_id,
        "book_hash": book_hash,
        "total_pages": info.get("pages", 0),
        "question_type": "none",
        "count_per_chapter": 0,
        "status": IngestJobStatus.queued.value,
        "chapters_done": 0,
        "questions_created": 0,
        "total_chapters": 0,
        "pages_done": pages_done,
        "progress_percent": int(100 * pages_done / max(info.get("pages") or 1, 1)),
        "current_chapter": None,
        "current_chapter_title": None,
        "progress_message": (
            f"Queued — resuming from page {pages_done + 1}." if resumed
            else "Queued for Library ingestion."
        ),
        "last_heartbeat_at": now,
        "error_message": None,
        "started_at": None,
        "completed_at": None,
        "created_at": now,
    }
    # If we're resuming an existing job_id, upsert; otherwise insert
    await db["ingest_jobs"].update_one(
        {"_id": job_id}, {"$set": job_doc}, upsert=True,
    )

    # Ensure a checkpoint stub exists with this job_id (so the worker can resume cleanly)
    if not checkpoint:
        await save_checkpoint(book_hash, {
            "book_id": book_id, "filename": filename, "job_id": job_id,
            "total_pages": info.get("pages", 0),
            "next_page": 0, "pages_done": 0, "chunks_stored": 0,
            "ocr_active": True, "status": "in_progress",
            "state": None,
        })

    # Store the PDF once in GridFS so the Celery message (and every re-queue)
    # carries only ids. Fall back to the inline payload if GridFS save failed.
    saved = await save_book_pdf(book_hash, filename, raw_bytes)
    pdf_b64 = "" if saved else base64.b64encode(raw_bytes).decode("utf-8")
    ingest_book_only_task.delay(job_id, pdf_b64, book_id, book_hash)

    return {
        "job_id": job_id,
        "filename": filename,
        "book_id": book_id,
        "book_hash": book_hash,
        "total_pages": info.get("pages"),
        "pages_done": pages_done,
        "progress_percent": int(100 * pages_done / max(info.get("pages") or 1, 1)),
        "status": "queued",
        "already_ingested": False,
        "resumed": resumed,
        "resumed_from_page": pages_done if resumed else None,
        "total_chapters": 0,
        "chapters_done": 0,
        "current_chapter": None,
        "current_chapter_title": None,
        "questions_created": 0,
        "progress_message": job_doc["progress_message"],
        "last_heartbeat_at": now.isoformat(),
        "error": None,
        "created_at": now.isoformat(),
        "started_at": None,
        "completed_at": None,
    }



@router.delete("/books/{book_hash}/cache", status_code=204)
async def clear_book_cache(
    book_hash: str,
    _: dict = Depends(require_instructor),
):
    """Delete the checkpoint AND any partial chunks for this book hash. Generated questions are kept."""
    from app.services.mongo_vector_store import delete_checkpoint, delete_book_chunks, delete_book_pdf
    await delete_checkpoint(book_hash)
    await delete_book_chunks(book_hash=book_hash)
    await delete_book_pdf(book_hash)
    return


@router.delete("/books/{book_id}/delete", status_code=204)
async def delete_book(
    book_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    """Permanently remove a book: deletes all chunks, questions, jobs, checkpoints, and the stored PDF."""
    from app.services.mongo_vector_store import (
        delete_book_chunks, delete_checkpoint, delete_book_pdf, delete_question_assets,
    )
    from app.services.math_index import delete_math_index
    from app.services.figure_index import delete_figure_index
    from app.services.table_index import delete_table_index
    from app.services.exercise_index import delete_exercise_index

    # Collect this book's content hashes before the chunks are gone, so the
    # matching checkpoints and GridFS PDFs can be cleaned up too.
    hashes = await db["pdf_chunks"].distinct("book_hash", {"book_id": book_id})
    await delete_book_chunks(book_id=book_id)
    await delete_math_index(book_id)
    await delete_figure_index(book_id)
    await delete_table_index(book_id)
    await delete_exercise_index(book_id)

    # Collect generated asset image ids AND question ids before the questions are
    # removed, so the GridFS images don't leak and so quizzes/submissions
    # pointing at these questions can be cascaded.
    asset_ids: list[str] = []
    question_ids: list[str] = []
    async for q in db["questions"].find(
        {"book_id": book_id}, {"assets": 1}
    ):
        question_ids.append(q["_id"])
        for a in q.get("assets", []) or []:
            if a.get("image_id"):
                asset_ids.append(a["image_id"])
    if asset_ids:
        await delete_question_assets(asset_ids)
    await db["questions"].delete_many({"book_id": book_id})

    # Cascade: pull the deleted question ids out of every quiz and remove their
    # submissions, so no quiz references a dead question and no orphan
    # submissions linger. Best-effort — failures here must not fail the delete.
    if question_ids:
        try:
            await db["quizzes"].update_many(
                {"question_ids": {"$in": question_ids}},
                {"$pull": {"question_ids": {"$in": question_ids}}},
            )
        except Exception as exc:
            logger.warning(f"[delete_book] quiz cascade failed (non-fatal): {exc}")
        try:
            await db["submissions"].delete_many({"question_id": {"$in": question_ids}})
        except Exception as exc:
            logger.warning(f"[delete_book] submission cascade failed (non-fatal): {exc}")
    # Job _ids are UUIDs — match on the book_id field, not _id
    await db["ingest_jobs"].delete_many({"book_id": book_id})
    for h in hashes:
        if h:
            await delete_checkpoint(h)
            await delete_book_pdf(h)
    return


# ── List ingested books ────────────────────────────────────────────────────────

@router.get("/books")
async def list_books(
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    """Return all books currently in the pdf_chunks collection with per-book stats."""
    pipeline = [
        {"$group": {
            "_id": "$book_id",
            "total_chunks": {"$sum": 1},
            "chapter_set": {"$addToSet": {"num": "$chapter_num", "title": "$chapter_title"}},
            "with_tables": {"$sum": {"$cond": ["$has_tables", 1, 0]}},
            "with_math":   {"$sum": {"$cond": ["$has_math",   1, 0]}},
            "with_images": {"$sum": {"$cond": ["$has_images", 1, 0]}},
            "ingested_at": {"$max": "$created_at"},
        }},
        {"$sort": {"ingested_at": -1}},
    ]
    raw = await db["pdf_chunks"].aggregate(pipeline).to_list(length=200)

    books = []
    for doc in raw:
        book_id = doc["_id"] or "unknown"
        # Deduplicate by chapter number — keep the shortest, cleanest title
        # (guards against TOC dot-leader variants slipping through)
        ch_by_num: dict[int, str] = {}
        for c in doc.get("chapter_set", []):
            num, title = c.get("num"), c.get("title", "")
            if not title or num is None:
                continue
            title = re.sub(r"(\s*\.\s*){2,}$", "", title).strip()
            if num not in ch_by_num or len(title) < len(ch_by_num[num]):
                ch_by_num[num] = title
        chapters = sorted(ch_by_num.items(), key=lambda x: x[0])
        # Try to find original filename from ingest_jobs
        job = await db["ingest_jobs"].find_one(
            {"_id": book_id},
            projection={"filename": 1},
        )
        display_name = (
            job.get("filename", "") if job else ""
        ) or book_id.replace("-", " ").replace("_", " ").title()

        # Specialist index build status (math/figure/table) so the UI can show
        # that retrieval quality is still ramping up after ingestion.
        index_builds = []
        async for ib in db["index_build_jobs"].find(
            {"book_id": book_id},
            projection={"_id": 0, "index": 1, "status": 1, "progress": 1, "error": 1},
        ):
            index_builds.append(ib)

        books.append({
            "book_id": book_id,
            "display_name": display_name,
            "total_chunks": doc["total_chunks"],
            "total_chapters": len(chapters),
            "chapters": [{"num": n, "title": t} for n, t in chapters[:30]],
            "with_tables": doc["with_tables"],
            "with_math": doc["with_math"],
            "with_images": doc["with_images"],
            "ingested_at": doc["ingested_at"].isoformat() if doc.get("ingested_at") else None,
            "index_builds": index_builds,
        })
    return {"books": books}


# ── Generate from existing DB book ────────────────────────────────────────────

@router.post("/generate/from-book")
async def generate_from_book(
    book_id: str = Query(...),
    question_type: str = Query("short_answer", enum=["mcq", "true_false", "short_answer"]),
    count_per_chapter: int = Query(10, ge=1, le=50),
    chapter_nums: Optional[str] = Query(None, description="Comma-separated chapter numbers, e.g. '1,3,5'. Omit for all chapters."),
    difficulty: str = Query("all", enum=["all", "easy", "medium", "hard"]),
    require_table: bool = Query(False, description="If true, every question must be built around a real data table from the chapter."),
    require_figure: bool = Query(False, description="If true, every question must be built around a real figure/graph from the chapter."),
    deepsearch: bool = Query(True, description="Run the DeepSearch refine pass (evidence-backed repair before the quality gate)."),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    """Generate questions from a book already in the Library. Supports chapter filtering and difficulty."""
    count = await db["pdf_chunks"].count_documents({"book_id": book_id})
    if count == 0:
        raise HTTPException(404, f"No chunks found for book_id '{book_id}'. Ingest the book first.")

    # Parse chapter_nums param
    chapter_list: Optional[List[int]] = None
    if chapter_nums:
        try:
            chapter_list = [int(n.strip()) for n in chapter_nums.split(",") if n.strip()]
        except ValueError:
            raise HTTPException(422, "chapter_nums must be comma-separated integers, e.g. '1,3,5'")

    now = datetime.now(timezone.utc)
    job_id = str(uuid.uuid4())
    diff_label = f" [{difficulty}]" if difficulty != "all" else ""
    ch_label = f" (Ch {chapter_nums})" if chapter_list else ""
    job_doc = {
        "_id": job_id,
        "filename": book_id,
        "total_pages": 0,
        "question_type": question_type,
        "count_per_chapter": count_per_chapter,
        "status": IngestJobStatus.queued.value,
        "chapters_done": 0,
        "questions_created": 0,
        "total_chapters": 0,
        "current_chapter": None,
        "current_chapter_title": None,
        "progress_message": f"Queued — generating{diff_label} questions from '{book_id}'{ch_label}.",
        "last_heartbeat_at": now,
        "error_message": None,
        "started_at": None,
        "completed_at": None,
        "created_at": now,
    }
    await db["ingest_jobs"].insert_one(job_doc)
    generate_from_book_task.delay(job_id, book_id, question_type, count_per_chapter, chapter_list, difficulty, require_table, require_figure, deepsearch)

    return {
        "job_id": job_id,
        "filename": book_id,
        "total_pages": 0,
        "status": "queued",
        "total_chapters": 0,
        "chapters_done": 0,
        "current_chapter": None,
        "current_chapter_title": None,
        "questions_created": 0,
        "progress_message": job_doc["progress_message"],
        "last_heartbeat_at": now.isoformat(),
        "error": None,
        "created_at": now.isoformat(),
        "started_at": None,
        "completed_at": None,
    }


# ── Quick generate (synchronous) ──────────────────────────────────────────────

@router.post("/generate", response_model=QuestionGenerateResponse)
async def generate_from_upload(
    file: UploadFile = File(...),
    question_type: str = Query("short_answer", enum=["mcq", "true_false", "short_answer"]),
    count: int = Query(20, ge=1, le=50),
    topic_filter: Optional[str] = Query(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    filename = (file.filename or "").lower()
    raw_bytes = await _read_upload_within_limit(file)

    chunks = None
    info: dict = {}

    if filename.endswith(".pdf"):
        info = await asyncio.to_thread(get_pdf_info, raw_bytes)
        chunks = await asyncio.to_thread(
            parse_pdf_into_chunks, raw_bytes, max_pages=settings.PDF_MAX_PAGES
        )
        if not chunks:
            raise HTTPException(422, "No usable text extracted.")
        questions_data = await generate_questions_from_chunks(chunks, question_type, count, topic_filter=topic_filter)
        # This quick path bypassed generate_questions()'s post-gen passes, so run
        # them here: drop un-answerable/incorrect questions via the quality gate,
        # then realise any figure-spec images (book_id/chapter_num unknown here —
        # that's fine, realize_figure_images tolerates None).
        from app.services.answer_verifier import verify_generated_questions
        from app.services.question_assets import realize_figure_images
        questions_data = await verify_generated_questions(questions_data)
        questions_data = await realize_figure_images(questions_data)
    elif filename.endswith(".txt"):
        content = raw_bytes.decode("utf-8", errors="ignore")
        if not content.strip():
            raise HTTPException(422, "Uploaded .txt file is empty.")
        questions_data = await generate_questions(content, question_type, count)
    else:
        raise HTTPException(415, "Unsupported file type. Upload a .pdf or .txt file.")

    if not questions_data:
        raise HTTPException(500, "LLM returned no questions.")

    created = []
    for q_data in questions_data:
        q_text = q_data.get("question_text", "").strip()
        if not q_text:
            continue
        m_answer = q_data.get("model_answer", "").strip()
        embedding = None
        try:
            embedding = await llm_service.embed(f"{q_text} {m_answer}")
        except Exception:
            pass

        doc = {
            "_id": str(uuid.uuid4()),
            "question_text": q_text,
            "question_type": q_data.get("question_type", question_type),
            "model_answer": m_answer,
            "rubric": q_data.get("rubric", ""),
            "max_marks": float(q_data.get("max_marks", 5)),
            "topic_tag": q_data.get("topic_tag", "General"),
            "difficulty": q_data.get("difficulty", "medium"),
            "correct_answer": q_data.get("correct_answer"),
            "source_page_range": q_data.get("_page_range"),
            "source_chunk": q_data.get("_source_chunk"),
            "assets": q_data.get("assets", []),
            "embedding": embedding,
            "assigned_student_ids": [],
            "created_at": datetime.now(timezone.utc),
        }
        await db["questions"].insert_one(doc)
        created.append(_q_out(doc))

    return {
        "generated": len(created),
        "source_file": file.filename,
        "source_pages": info.get("pages"),
        "chunks_processed": len(chunks) if chunks is not None else None,
        "topics_covered": list({q["topic_tag"] for q in created}),
        "questions": created,
    }


# ── Job status ────────────────────────────────────────────────────────────────

def _job_out(job: dict) -> dict:
    def _iso(v):
        return v.isoformat() if v else None
    return {
        "job_id": job["_id"],
        "filename": job.get("filename", ""),
        "book_hash": job.get("book_hash"),
        "total_pages": job.get("total_pages", 0),
        "pages_done": job.get("pages_done", 0),
        "progress_percent": job.get("progress_percent", 0),
        "status": job["status"],
        "total_chapters": job.get("total_chapters", 0),
        "chapters_done": job.get("chapters_done", 0),
        "current_chapter": job.get("current_chapter"),
        "current_chapter_title": job.get("current_chapter_title"),
        "questions_created": job.get("questions_created", 0),
        "progress_message": job.get("progress_message"),
        "last_heartbeat_at": _iso(job.get("last_heartbeat_at")),
        "error": job.get("error_message"),
        "created_at": _iso(job.get("created_at")),
        "started_at": _iso(job.get("started_at")),
        "completed_at": _iso(job.get("completed_at")),
    }


@router.get("/jobs")
async def list_jobs(
    status: Optional[str] = Query(None, description="Comma-separated statuses, e.g. 'queued,processing'"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    """List recent ingest/generation jobs. Filter by status for active-job polling."""
    filt: dict = {}
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if statuses:
            filt["status"] = {"$in": statuses}
    jobs = await db["ingest_jobs"].find(filt).sort("created_at", -1).limit(limit).to_list(length=limit)
    return [_job_out(j) for j in jobs]


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    job = await db["ingest_jobs"].find_one({"_id": job_id})
    if not job:
        raise HTTPException(404, f"Job {job_id} not found.")
    return {
        "job_id": job["_id"],
        "filename": job["filename"],
        "book_hash": job.get("book_hash"),
        "total_pages": job.get("total_pages", 0),
        "pages_done": job.get("pages_done", 0),
        "progress_percent": job.get("progress_percent", 0),
        "status": job["status"],
        "total_chapters": job.get("total_chapters", 0),
        "chapters_done": job.get("chapters_done", 0),
        "current_chapter": job.get("current_chapter"),
        "current_chapter_title": job.get("current_chapter_title"),
        "questions_created": job.get("questions_created", 0),
        "progress_message": job.get("progress_message"),
        "last_heartbeat_at": job["last_heartbeat_at"].isoformat() if job.get("last_heartbeat_at") else None,
        "error": job.get("error_message"),
        "created_at": job["created_at"].isoformat() if job.get("created_at") else None,
        "started_at": job["started_at"].isoformat() if job.get("started_at") else None,
        "completed_at": job["completed_at"].isoformat() if job.get("completed_at") else None,
    }


@router.get("/jobs/{job_id}/stream")
async def stream_job_status(
    job_id: str,
    token: str = Query(...),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Server-Sent Events (SSE) endpoint to stream job progress.

    Browsers' EventSource API cannot send Authorization headers, so the JWT is
    passed as a `token` query parameter and validated explicitly here.
    """
    from fastapi.responses import StreamingResponse
    import asyncio
    import json

    try:
        claims = decode_token(token)
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")
    if not claims.get("sub"):
        raise HTTPException(401, "Invalid or expired token")
    if claims.get("role") != "instructor":
        raise HTTPException(403, "Instructor access required")

    async def event_generator():
        last_status = None
        last_msg = None
        last_done = -1
        last_pages = -1

        # Stream for up to 1 hour
        for _ in range(3600):
            job = await db["ingest_jobs"].find_one({"_id": job_id})
            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break

            status = job.get("status")
            msg = job.get("progress_message")
            done = job.get("chapters_done", 0)
            pages_done = job.get("pages_done", 0)

            if (status != last_status or msg != last_msg
                    or done != last_done or pages_done != last_pages):
                payload = {
                    "job_id": job["_id"],
                    "status": status,
                    "total_chapters": job.get("total_chapters", 0),
                    "chapters_done": done,
                    "total_pages": job.get("total_pages", 0),
                    "pages_done": pages_done,
                    "progress_percent": job.get("progress_percent", 0),
                    "progress_message": msg,
                    "questions_created": job.get("questions_created", 0),
                    "error": job.get("error_message"),
                }
                yield f"data: {json.dumps(payload)}\n\n"

                last_status = status
                last_msg = msg
                last_done = done
                last_pages = pages_done

            if status in [IngestJobStatus.done.value, IngestJobStatus.failed.value]:
                break

            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# ── Generated asset images ─────────────────────────────────────────────────────
# Placed before the `/{question_id}` catch-all so "assets" is never matched as a
# question id. Streams the PNG from GridFS. Auth accepts either an Authorization
# header OR a ?token= query param, because <img> tags can't send headers (same
# pattern as the SSE endpoint). Any authenticated user may load images.

@router.get("/assets/{image_id}")
async def get_question_asset_image(
    image_id: str,
    token: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    from fastapi.responses import Response
    from app.services.mongo_vector_store import load_question_asset

    raw = token
    if not raw and authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    if not raw:
        raise HTTPException(401, "Authentication required")
    try:
        claims = decode_token(raw)
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")
    if not claims.get("sub"):
        raise HTTPException(401, "Invalid or expired token")

    png = await load_question_asset(image_id)
    if png is None:
        raise HTTPException(404, "Asset not found")
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── Count / topics / assessment ───────────────────────────────────────────────

@router.get("/count")
async def count_questions(
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    total = await db["questions"].count_documents({})
    return {"total": total}


@router.get("/topics")
async def list_topics(
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    pipeline = [
        {"$group": {"_id": "$topic_tag", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    rows = await db["questions"].aggregate(pipeline).to_list(length=500)
    return [{"topic": r["_id"], "count": r["count"]} for r in rows]


@router.get("/assessment", response_model=List[AssessmentQuestionOut])
async def list_assessment_questions(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncIOMotorDatabase = Depends(get_db),
    claims: dict = Depends(get_current_user),
):
    if claims.get("role") == "instructor":
        cursor = db["questions"].find({}, {"embedding": 0}).sort("created_at", -1).skip(skip).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [_q_out(d) for d in docs]

    student_id = claims["sub"]
    # A student sees questions assigned directly (legacy) OR via any assigned quiz.
    from app.api.v1.quizzes import student_quiz_question_ids
    quiz_ids = await student_quiz_question_ids(db, student_id)
    query = {"$or": [
        {"assigned_student_ids": student_id},
        {"_id": {"$in": list(quiz_ids)}},
    ]}
    doc = await db["questions"].find(
        query, {"embedding": 0}
    ).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)
    return [_q_out(d) for d in doc]


@router.get("/", response_model=List[QuestionOut])
async def list_questions(
    topic: Optional[str] = None,
    difficulty: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    filt: dict = {}
    if topic:
        filt["topic_tag"] = topic
    if difficulty:
        filt["difficulty"] = difficulty
    cursor = db["questions"].find(filt, {"embedding": 0}).sort("created_at", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [_q_out(d) for d in docs]


# ── Assignee management ───────────────────────────────────────────────────────

@router.get("/{question_id}/assignees", response_model=QuestionAssigneeOut)
async def get_question_assignees(
    question_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    doc = await db["questions"].find_one({"_id": question_id}, {"assigned_student_ids": 1})
    if not doc:
        raise HTTPException(404, "Question not found")
    return {"question_id": question_id, "student_ids": doc.get("assigned_student_ids", [])}


@router.put("/{question_id}/assignees", response_model=QuestionAssigneeOut)
async def update_question_assignees(
    question_id: str,
    payload: QuestionAssigneeUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    if not await db["questions"].find_one({"_id": question_id}):
        raise HTTPException(404, "Question not found")

    student_ids = list(dict.fromkeys(str(s) for s in payload.student_ids))
    if student_ids:
        existing = await db["users"].find(
            {"_id": {"$in": student_ids}, "role": "student"}, {"_id": 1}
        ).to_list(length=len(student_ids))
        found = {d["_id"] for d in existing}
        missing = [s for s in student_ids if s not in found]
        if missing:
            raise HTTPException(400, f"Invalid student id(s): {', '.join(missing)}")

    await db["questions"].update_one(
        {"_id": question_id},
        {"$set": {"assigned_student_ids": student_ids}},
    )
    return {"question_id": question_id, "student_ids": student_ids}


# ── Single question CRUD ──────────────────────────────────────────────────────

@router.get("/{question_id}", response_model=QuestionOut)
async def get_question(
    question_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    doc = await db["questions"].find_one({"_id": question_id}, {"embedding": 0})
    if not doc:
        raise HTTPException(404, "Question not found")
    return _q_out(doc)


def _derive_correct_answer(question_type: str, question_text: str, model_answer: str) -> str | None:
    """Structured answer key for objective questions (marking compares directly)."""
    from app.services.rag_pipeline import _extract_mcq_correct, _extract_true_false
    if question_type == "mcq":
        return _extract_mcq_correct(question_text, model_answer)
    if question_type == "true_false":
        return _extract_true_false(model_answer)
    return None


@router.post("/", response_model=QuestionOut, status_code=201)
async def create_question(
    payload: QuestionCreate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    embedding = await llm_service.embed(f"{payload.question_text} {payload.model_answer}")
    qtype = getattr(payload.question_type, "value", str(payload.question_type))
    doc = {
        "_id": str(uuid.uuid4()),
        **payload.model_dump(),
        "correct_answer": _derive_correct_answer(qtype, payload.question_text, payload.model_answer),
        "assets": [],
        "embedding": embedding,
        "assigned_student_ids": [],
        "created_at": datetime.now(timezone.utc),
    }
    await db["questions"].insert_one(doc)
    return _q_out(doc)


@router.put("/{question_id}", response_model=QuestionOut)
async def update_question(
    question_id: str,
    payload: QuestionUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    if not await db["questions"].find_one({"_id": question_id}):
        raise HTTPException(404, "Question not found")
    data = payload.model_dump()
    qtype = getattr(payload.question_type, "value", str(payload.question_type))
    data["correct_answer"] = _derive_correct_answer(qtype, payload.question_text, payload.model_answer)
    data["embedding"] = await llm_service.embed(f"{payload.question_text} {payload.model_answer}")
    await db["questions"].update_one({"_id": question_id}, {"$set": data})
    doc = await db["questions"].find_one({"_id": question_id}, {"embedding": 0})
    return _q_out(doc)


@router.delete("/{question_id}", status_code=204)
async def delete_question(
    question_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    from app.services.mongo_vector_store import delete_question_assets

    doc = await db["questions"].find_one({"_id": question_id}, {"assets": 1})
    result = await db["questions"].delete_one({"_id": question_id})
    if result.deleted_count == 0:
        raise HTTPException(404, "Question not found")
    asset_ids = [
        a["image_id"] for a in (doc or {}).get("assets", []) or []
        if a.get("image_id")
    ]
    if asset_ids:
        await delete_question_assets(asset_ids)

    # Cascade: drop this id from any quiz that references it, and remove its
    # submissions, so no quiz points at a dead question and no orphan
    # submissions linger. Best-effort — a failure here must not fail the delete.
    try:
        await db["quizzes"].update_many(
            {"question_ids": question_id},
            {"$pull": {"question_ids": question_id}},
        )
    except Exception as exc:
        logger.warning(f"[delete_question] quiz cascade failed (non-fatal): {exc}")
    try:
        await db["submissions"].delete_many({"question_id": question_id})
    except Exception as exc:
        logger.warning(f"[delete_question] submission cascade failed (non-fatal): {exc}")
