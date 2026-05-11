from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database import get_db
from app.core.security import decode_token
from app.models.models import Submission, User, Question
from app.schemas.schemas import SubmissionCreate, SubmissionOut
from app.tasks.marking_tasks import mark_submission_task
from typing import List
import uuid

router = APIRouter()


@router.post("/", response_model=SubmissionOut, status_code=201)
async def submit_answer(
    payload: SubmissionCreate,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing or invalid authorization header")

    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = decode_token(token)
        user_id = claims.get("sub")
        if not user_id:
            raise HTTPException(401, "Invalid token")
    except Exception:
        raise HTTPException(401, "Invalid token")

    user = await db.get(User, uuid.UUID(user_id))
    if not user:
        raise HTTPException(401, "User not found")

    submission = Submission(
        student_id=user.id,
        question_id=payload.question_id,
        answer_text=payload.answer_text,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)

    # Dispatch async marking job
    mark_submission_task.delay(str(submission.id))

    question = await db.get(Question, submission.question_id)
    return {
        **submission.__dict__,
        "question_text": question.question_text if question else None,
        "question_type": question.question_type if question else None,
        "max_marks": question.max_marks if question else None,
    }


@router.get("/", response_model=List[SubmissionOut])
async def list_submissions(
    flagged_only: bool = False, db: AsyncSession = Depends(get_db)
):
    q = select(Submission, Question).join(Question, Submission.question_id == Question.id)
    if flagged_only:
        q = q.where(Submission.is_flagged == True)
    result = await db.execute(q)
    rows = result.all()
    return [
        {
            **s.__dict__,
            "question_text": q.question_text,
            "question_type": q.question_type,
            "max_marks": q.max_marks,
        }
        for s, q in rows
    ]


@router.get("/{submission_id}", response_model=SubmissionOut)
async def get_submission(submission_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Submission, Question)
        .join(Question, Submission.question_id == Question.id)
        .where(Submission.id == submission_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(404, "Submission not found")
    s, q = row
    return {
        **s.__dict__,
        "question_text": q.question_text,
        "question_type": q.question_type,
        "max_marks": q.max_marks,
    }
