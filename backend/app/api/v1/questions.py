from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.core.database import get_db
from app.models.models import Question, IngestJob, IngestJobStatus
from app.schemas.schemas import QuestionCreate, QuestionUpdate, QuestionOut
from app.services.question_generator import generate_questions, generate_questions_from_chunks
from app.services.llm_service import llm_service
from app.services.pdf_service import parse_pdf_into_chunks, extract_text_from_pdf, get_pdf_info
from app.core.config import settings
from app.tasks.ingest_tasks import ingest_pdf_task
from typing import List, Optional
import uuid

router = APIRouter()


# ── Standard CRUD ──────────────────────────────────────────────────────────────

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
    result = await db.execute(q)
    return result.scalars().all()


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


# ── Quick generate (small file / single topic, synchronous) ───────────────────

@router.post("/generate")
async def generate_from_upload(
    file: UploadFile = File(...),
    question_type: str = Query("short_answer", enum=["mcq", "true_false", "short_answer"]),
    count: int = Query(20, ge=1, le=50),
    topic_filter: Optional[str] = Query(None, description="Filter to a specific chapter topic"),
    db: AsyncSession = Depends(get_db),
):
    """
    Synchronous generation from a .pdf or .txt file.
    For PDFs: deep chunk-aware processing — understands chapter structure,
    filters exercises, preserves formulas, generates per-topic questions.
    Recommended for focused requests (single chapter, ≤50 questions).
    For large textbooks (whole book), use /generate/async instead.
    """
    raw_bytes = await file.read()
    filename = (file.filename or "").lower()

    max_bytes = settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024
    if len(raw_bytes) > max_bytes:
        raise HTTPException(413, f"File exceeds {settings.UPLOAD_MAX_SIZE_MB} MB limit.")

    info: dict = {}

    if filename.endswith(".pdf"):
        info = get_pdf_info(raw_bytes)
        # Deep chunk-aware parsing
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

    # Persist to DB
    created = []
    for q_data in questions_data:
        q = Question(
            question_text=q_data.get("question_text", ""),
            question_type=q_data.get("question_type", question_type),
            model_answer=q_data.get("model_answer", ""),
            rubric=q_data.get("rubric", ""),
            max_marks=float(q_data.get("max_marks", 5)),
            topic_tag=q_data.get("topic_tag", "Statistics"),
            difficulty=q_data.get("difficulty", "medium"),
        )
        if not q.question_text.strip():
            continue
        q.embedding = await llm_service.embed(f"{q.question_text} {q.model_answer}")
        db.add(q)
        created.append(q)

    await db.commit()

    return {
        "generated": len(created),
        "source_file": file.filename,
        "source_pages": info.get("pages"),
        "chunks_processed": len(chunks) if filename.endswith(".pdf") else None,
        "topics_covered": list({q.topic_tag for q in created}),
    }


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
    The Celery worker processes the entire PDF chapter-by-chapter in the background.
    Poll GET /questions/jobs/{job_id} for status.

    Use this for large textbooks (100+ pages) where synchronous generation
    would time out.
    """
    raw_bytes = await file.read()
    filename = (file.filename or "").lower()

    max_bytes = settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024
    if len(raw_bytes) > max_bytes:
        raise HTTPException(413, f"File exceeds {settings.UPLOAD_MAX_SIZE_MB} MB limit.")

    if not filename.endswith(".pdf"):
        raise HTTPException(415, "Async ingestion only supports PDF files.")

    info = get_pdf_info(raw_bytes)

    # Create a DB job record
    job = IngestJob(
        filename=file.filename or "upload.pdf",
        total_pages=info.get("pages", 0),
        question_type=question_type,
        count_per_chapter=count_per_chapter,
        status=IngestJobStatus.queued,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Dispatch Celery task
    ingest_pdf_task.delay(str(job.id), raw_bytes, question_type, count_per_chapter)

    return {
        "job_id": str(job.id),
        "filename": file.filename,
        "total_pages": info.get("pages"),
        "status": "queued",
        "message": "Processing started. Poll /questions/jobs/{job_id} for progress.",
    }


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Poll the status of an async PDF ingestion job."""
    job = await db.get(IngestJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "job_id": str(job.id),
        "filename": job.filename,
        "status": job.status,
        "total_pages": job.total_pages,
        "chapters_done": job.chapters_done,
        "questions_created": job.questions_created,
        "error": job.error_message,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }
