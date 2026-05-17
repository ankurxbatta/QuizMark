from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.core.database import get_db
from app.models.models import Question, IngestJob, IngestJobStatus
from app.schemas.schemas import QuestionCreate, QuestionUpdate, QuestionOut, QuestionGenerateResponse
from app.services.question_generator import generate_questions, generate_questions_from_chunks
from app.services.llm_service import llm_service
from app.services.pdf_service import parse_pdf_into_chunks, extract_text_from_pdf, get_pdf_info, extract_chapters_from_pdf
from app.core.config import settings
from app.tasks.ingest_tasks import ingest_pdf_task
from typing import List, Optional
import base64
import uuid
from datetime import datetime, timezone

router = APIRouter()

# ═══════════════════════════════════════════════════════════════════════════════
# IMPORTANT: All named sub-routes (/generate, /generate/async, /chapters,
# /count, /topics, /jobs/...) MUST be defined BEFORE /{question_id}.
# FastAPI matches routes in definition order, and /{question_id} would greedily
# capture any path segment as a UUID string (and fail) if defined first.
# ═══════════════════════════════════════════════════════════════════════════════


# ── Chapter extraction (dynamic, from uploaded PDF) ───────────────────────────

@router.post("/chapters")
async def extract_chapters(
    file: UploadFile = File(...),
):
    """
    Scan a PDF and return the chapters found in it.
    Used by the frontend to populate the chapter filter dropdown dynamically.
    No DB interaction — pure PDF scan.
    """
    raw_bytes = await file.read()
    filename = (file.filename or "").lower()
    if not filename.endswith(".pdf"):
        raise HTTPException(415, "Only PDF files are supported.")
    max_bytes = settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024
    if len(raw_bytes) > max_bytes:
        raise HTTPException(413, f"File exceeds {settings.UPLOAD_MAX_SIZE_MB} MB limit.")
    chapters = extract_chapters_from_pdf(raw_bytes)
    return {"chapters": chapters}


# ── Async full-book ingest (background Celery job) ────────────────────────────

@router.post("/generate/async")
async def generate_async(
    file: UploadFile = File(...),
    question_type: str = Query("short_answer", enum=["mcq", "true_false", "short_answer"]),
    count_per_chapter: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """
    Async full-textbook ingestion. Returns a job_id immediately.
    Poll GET /questions/jobs/{job_id} for status.
    """
    raw_bytes = await file.read()
    filename = (file.filename or "").lower()

    max_bytes = settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024
    if len(raw_bytes) > max_bytes:
        raise HTTPException(413, f"File exceeds {settings.UPLOAD_MAX_SIZE_MB} MB limit.")

    if not filename.endswith(".pdf"):
        raise HTTPException(415, "Async ingestion only supports PDF files.")

    info = get_pdf_info(raw_bytes)

    job = IngestJob(
        filename=file.filename or "upload.pdf",
        total_pages=info.get("pages", 0),
        question_type=question_type,
        count_per_chapter=count_per_chapter,
        status=IngestJobStatus.queued,
        progress_message="Queued for worker pickup.",
        last_heartbeat_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    pdf_b64 = base64.b64encode(raw_bytes).decode("utf-8")
    ingest_pdf_task.delay(str(job.id), pdf_b64, question_type, count_per_chapter)

    return {
        "job_id": str(job.id),
        "filename": file.filename,
        "total_pages": info.get("pages"),
        "status": "queued",
        "total_chapters": 0,
        "chapters_done": 0,
        "current_chapter": None,
        "current_chapter_title": None,
        "questions_created": 0,
        "progress_message": "Queued for worker pickup.",
        "last_heartbeat_at": job.last_heartbeat_at.isoformat() if job.last_heartbeat_at else None,
        "error": None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": None,
        "completed_at": None,
        "message": "Processing started. Poll /questions/jobs/{job_id} for progress.",
    }


# ── Quick generate (synchronous) ──────────────────────────────────────────────

@router.post("/generate", response_model=QuestionGenerateResponse)
async def generate_from_upload(
    file: UploadFile = File(...),
    question_type: str = Query("short_answer", enum=["mcq", "true_false", "short_answer"]),
    count: int = Query(20, ge=1, le=50),
    topic_filter: Optional[str] = Query(None, description="Filter to a specific chapter topic"),
    db: AsyncSession = Depends(get_db),
):
    """
    Synchronous generation from a .pdf or .txt file.
    For large textbooks use /generate/async instead.
    """
    raw_bytes = await file.read()
    filename = (file.filename or "").lower()

    max_bytes = settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024
    if len(raw_bytes) > max_bytes:
        raise HTTPException(413, f"File exceeds {settings.UPLOAD_MAX_SIZE_MB} MB limit.")

    chunks = None
    info: dict = {}

    if filename.endswith(".pdf"):
        info = get_pdf_info(raw_bytes)
        chunks = parse_pdf_into_chunks(raw_bytes, max_pages=settings.PDF_MAX_PAGES)
        if not chunks:
            raise HTTPException(422, "No usable text extracted. Is the PDF text-based?")
        questions_data = await generate_questions_from_chunks(
            chunks, question_type, count, topic_filter=topic_filter
        )

    elif filename.endswith(".txt"):
        content = raw_bytes.decode("utf-8", errors="ignore")
        if not content.strip():
            raise HTTPException(422, "Uploaded .txt file is empty.")
        questions_data = await generate_questions(content, question_type, count)

    else:
        raise HTTPException(415, "Unsupported file type. Upload a .pdf or .txt file.")

    if not questions_data:
        raise HTTPException(500, "LLM returned no questions. Try again or use a different chapter.")

    created = []
    for q_data in questions_data:
        q = Question(
            question_text=q_data.get("question_text", ""),
            question_type=q_data.get("question_type", question_type),
            model_answer=q_data.get("model_answer", ""),
            rubric=q_data.get("rubric", ""),
            max_marks=float(q_data.get("max_marks", 5)),
            topic_tag=q_data.get("topic_tag", "General"),
            difficulty=q_data.get("difficulty", "medium"),
            source_page_range=q_data.get("_page_range"),
            source_chunk=q_data.get("_source_chunk"),
        )
        if not q.question_text.strip():
            continue
        try:
            q.embedding = await llm_service.embed(f"{q.question_text} {q.model_answer}")
        except Exception as exc:
            print(f"[GEN] embedding failed for generated question: {exc}")
            q.embedding = None
        db.add(q)
        created.append(q)

    await db.commit()

    for q in created:
        await db.refresh(q)

    return {
        "generated": len(created),
        "source_file": file.filename,
        "source_pages": info.get("pages"),
        "chunks_processed": len(chunks) if chunks is not None else None,
        "topics_covered": list({q.topic_tag for q in created}),
        "questions": created,
    }


# ── Job status polling ────────────────────────────────────────────────────────

@router.get("/jobs/{job_id}")
async def get_job_status(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Poll the status of a background ingestion job."""
    job: IngestJob | None = await db.get(IngestJob, job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found.")
    return {
        "job_id": str(job.id),
        "filename": job.filename,
        "total_pages": job.total_pages,
        "status": job.status.value,
        "total_chapters": job.total_chapters,
        "chapters_done": job.chapters_done,
        "current_chapter": job.current_chapter,
        "current_chapter_title": job.current_chapter_title,
        "questions_created": job.questions_created,
        "progress_message": job.progress_message,
        "last_heartbeat_at": job.last_heartbeat_at.isoformat() if job.last_heartbeat_at else None,
        "error": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


# ── Standard read/list/count/topics (before /{question_id}) ──────────────────

@router.get("/count")
async def count_questions(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(func.count(Question.id)))
    return {"total": result.scalar()}


@router.get("/topics")
async def list_topics(db: AsyncSession = Depends(get_db)):
    """Return all distinct topic tags in the Q&A bank."""
    result = await db.execute(
        select(Question.topic_tag, func.count(Question.id).label("count"))
        .group_by(Question.topic_tag)
        .order_by(func.count(Question.id).desc())
    )
    return [{"topic": r.topic_tag, "count": r.count} for r in result.all()]


@router.get("/", response_model=List[QuestionOut])
async def list_questions(
    topic: Optional[str] = None,
    difficulty: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    q = select(Question)
    if topic:
        q = q.where(Question.topic_tag == topic)
    if difficulty:
        q = q.where(Question.difficulty == difficulty)
    result = await db.execute(q.order_by(Question.created_at.desc()))
    return result.scalars().all()


# ── /{question_id} routes LAST — must not shadow the named routes above ────────

@router.get("/{question_id}", response_model=QuestionOut)
async def get_question(question_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(404, "Question not found")
    return q


@router.post("/", response_model=QuestionOut, status_code=201)
async def create_question(payload: QuestionCreate, db: AsyncSession = Depends(get_db)):
    question = Question(**payload.model_dump())
    question.embedding = await llm_service.embed(
        f"{payload.question_text} {payload.model_answer}"
    )
    db.add(question)
    await db.commit()
    await db.refresh(question)
    return question


@router.put("/{question_id}", response_model=QuestionOut)
async def update_question(
    question_id: uuid.UUID,
    payload: QuestionUpdate,
    db: AsyncSession = Depends(get_db),
):
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(404, "Question not found")
    for field, value in payload.model_dump().items():
        setattr(q, field, value)
    q.embedding = await llm_service.embed(f"{q.question_text} {q.model_answer}")
    await db.commit()
    await db.refresh(q)
    return q


@router.delete("/{question_id}", status_code=204)
async def delete_question(question_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(404, "Question not found")
    await db.delete(q)
    await db.commit()
