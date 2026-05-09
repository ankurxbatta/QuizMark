from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.core.database import get_db
from app.models.models import Question
from app.schemas.schemas import QuestionCreate, QuestionUpdate, QuestionOut
from app.services.question_generator import generate_questions
from app.services.llm_service import llm_service
from typing import List, Optional
import uuid

router = APIRouter()


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
    question_id: uuid.UUID, payload: QuestionUpdate, db: AsyncSession = Depends(get_db)
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


@router.post("/generate")
async def generate_from_upload(
    file: UploadFile = File(...),
    question_type: str = Query("short_answer", enum=["mcq", "true_false", "short_answer"]),
    count: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    content = (await file.read()).decode("utf-8", errors="ignore")
    questions = await generate_questions(content, question_type, count)

    created = []
    for q_data in questions:
        q = Question(**{k: v for k, v in q_data.items() if hasattr(Question, k)})
        q.embedding = await llm_service.embed(f"{q.question_text} {q.model_answer}")
        db.add(q)
        created.append(q)

    await db.commit()
    return {"generated": len(created)}
