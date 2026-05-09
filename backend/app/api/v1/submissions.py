from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database import get_db
from app.models.models import Submission
from app.schemas.schemas import SubmissionCreate, SubmissionOut
from app.tasks.marking_tasks import mark_submission_task
from typing import List
import uuid

router = APIRouter()


@router.post("/", response_model=SubmissionOut, status_code=201)
async def submit_answer(payload: SubmissionCreate, db: AsyncSession = Depends(get_db)):
    # TODO: inject authenticated student_id from JWT
    submission = Submission(
        student_id=uuid.uuid4(),  # replace with auth dependency
        question_id=payload.question_id,
        answer_text=payload.answer_text,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)

    # Dispatch async marking job
    mark_submission_task.delay(str(submission.id))

    return submission


@router.get("/", response_model=List[SubmissionOut])
async def list_submissions(
    flagged_only: bool = False, db: AsyncSession = Depends(get_db)
):
    q = select(Submission)
    if flagged_only:
        q = q.where(Submission.is_flagged == True)
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/{submission_id}", response_model=SubmissionOut)
async def get_submission(submission_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    s = await db.get(Submission, submission_id)
    if not s:
        raise HTTPException(404, "Submission not found")
    return s
