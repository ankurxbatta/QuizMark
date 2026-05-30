import uuid
from datetime import datetime, timezone
from typing import List, Optional
import base64

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_db
from app.core.security import require_instructor, get_current_user
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
from app.services.pdf_service import parse_pdf_into_chunks, extract_text_from_pdf, get_pdf_info, extract_chapters_from_pdf
from app.tasks.ingest_tasks import ingest_pdf_task, generate_from_book_task, ingest_book_only_task

router = APIRouter()


def _q_out(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = doc.pop("_id")
    doc.setdefault("assigned_student_ids", [])
    doc.setdefault("source_page_range", None)
    doc.setdefault("source_chunk", None)
    doc.pop("embedding", None)
    return doc


# ── Chapter extraction ─────────────────────────────────────────────────────────

@router.post("/chapters")
async def extract_chapters(
    file: UploadFile = File(...),
    _: dict = Depends(require_instructor),
):
    raw_bytes = await file.read()
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(415, "Only PDF files are supported.")
    if len(raw_bytes) > settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.UPLOAD_MAX_SIZE_MB} MB limit.")
    return {"chapters": extract_chapters_from_pdf(raw_bytes)}


# ── Async full-book ingest ─────────────────────────────────────────────────────

@router.post("/generate/async")
async def generate_async(
    file: UploadFile = File(...),
    question_type: str = Query("short_answer", enum=["mcq", "true_false", "short_answer"]),
    count_per_chapter: int = Query(10, ge=1, le=50),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    raw_bytes = await file.read()
    filename = (file.filename or "").lower()
    if len(raw_bytes) > settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.UPLOAD_MAX_SIZE_MB} MB limit.")
    if not filename.endswith(".pdf"):
        raise HTTPException(415, "Async ingestion only supports PDF files.")

    info = get_pdf_info(raw_bytes)
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

    import re as _re
    doc = raw[0]
    ch_by_num: dict[int, str] = {}
    for c in doc.get("chapter_set", []):
        num, title = c.get("num"), c.get("title", "")
        if not title or num is None:
            continue
        title = _re.sub(r"(\s*\.\s*){2,}$", "", title).strip()
        if num not in ch_by_num or len(title) < len(ch_by_num[num]):
            ch_by_num[num] = title
    chapters = sorted(ch_by_num.items(), key=lambda x: x[0])

    job = await db["ingest_jobs"].find_one({"_id": book_id}, projection={"filename": 1})
    display_name = (job.get("filename", "") if job else "") or book_id.replace("-", " ").replace("_", " ").title()

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
    """Upload a PDF and store it in the Library (pdf_chunks). No questions generated yet."""
    raw_bytes = await file.read()
    filename = (file.filename or "upload.pdf")
    if len(raw_bytes) > settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.UPLOAD_MAX_SIZE_MB} MB limit.")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(415, "Only PDF files are supported.")

    info = get_pdf_info(raw_bytes)
    now = datetime.now(timezone.utc)
    job_id = str(uuid.uuid4())
    # Use filename (sans extension) as the book_id for a readable Library entry
    book_id = filename.rsplit(".", 1)[0]

    job_doc = {
        "_id": job_id,
        "filename": filename,
        "book_id": book_id,
        "total_pages": info.get("pages", 0),
        "question_type": "none",
        "count_per_chapter": 0,
        "status": IngestJobStatus.queued.value,
        "chapters_done": 0,
        "questions_created": 0,
        "total_chapters": 0,
        "current_chapter": None,
        "current_chapter_title": None,
        "progress_message": "Queued for Library ingestion.",
        "last_heartbeat_at": now,
        "error_message": None,
        "started_at": None,
        "completed_at": None,
        "created_at": now,
    }
    await db["ingest_jobs"].insert_one(job_doc)

    pdf_b64 = base64.b64encode(raw_bytes).decode("utf-8")
    ingest_book_only_task.delay(job_id, pdf_b64, book_id)

    return {
        "job_id": job_id,
        "filename": filename,
        "book_id": book_id,
        "total_pages": info.get("pages"),
        "status": "queued",
        "total_chapters": 0,
        "chapters_done": 0,
        "current_chapter": None,
        "current_chapter_title": None,
        "questions_created": 0,
        "progress_message": "Queued for Library ingestion.",
        "last_heartbeat_at": now.isoformat(),
        "error": None,
        "created_at": now.isoformat(),
        "started_at": None,
        "completed_at": None,
    }


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
        import re as _re
        ch_by_num: dict[int, str] = {}
        for c in doc.get("chapter_set", []):
            num, title = c.get("num"), c.get("title", "")
            if not title or num is None:
                continue
            title = _re.sub(r"(\s*\.\s*){2,}$", "", title).strip()
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
    generate_from_book_task.delay(job_id, book_id, question_type, count_per_chapter, chapter_list, difficulty)

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
    raw_bytes = await file.read()
    filename = (file.filename or "").lower()
    if len(raw_bytes) > settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.UPLOAD_MAX_SIZE_MB} MB limit.")

    chunks = None
    info: dict = {}

    if filename.endswith(".pdf"):
        info = get_pdf_info(raw_bytes)
        chunks = parse_pdf_into_chunks(raw_bytes, max_pages=settings.PDF_MAX_PAGES)
        if not chunks:
            raise HTTPException(422, "No usable text extracted.")
        questions_data = await generate_questions_from_chunks(chunks, question_type, count, topic_filter=topic_filter)
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
            "source_page_range": q_data.get("_page_range"),
            "source_chunk": q_data.get("_source_chunk"),
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
        "total_pages": job.get("total_pages", 0),
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
    db: AsyncIOMotorDatabase = Depends(get_db),
    claims: dict = Depends(get_current_user),
):
    if claims.get("role") == "instructor":
        cursor = db["questions"].find({}, {"embedding": 0}).sort("created_at", -1)
        docs = await cursor.to_list(length=2000)
        return [_q_out(d) for d in docs]

    student_id = claims["sub"]
    doc = await db["questions"].find(
        {"assigned_student_ids": student_id}, {"embedding": 0}
    ).sort("created_at", -1).to_list(length=2000)
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


@router.post("/", response_model=QuestionOut, status_code=201)
async def create_question(
    payload: QuestionCreate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    embedding = await llm_service.embed(f"{payload.question_text} {payload.model_answer}")
    doc = {
        "_id": str(uuid.uuid4()),
        **payload.model_dump(),
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
    result = await db["questions"].delete_one({"_id": question_id})
    if result.deleted_count == 0:
        raise HTTPException(404, "Question not found")
