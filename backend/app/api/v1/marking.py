from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database import get_db
from app.models.models import Submission, AuditLog
from app.tasks.marking_tasks import mark_submission_task
from app.schemas.schemas import OverrideRequest, SubmissionOut
import uuid
from datetime import datetime

router = APIRouter()


@router.put("/{submission_id}/override", response_model=SubmissionOut)
async def override_mark(
    submission_id: uuid.UUID,
    payload: OverrideRequest,
    db: AsyncSession = Depends(get_db),
):
    submission = await db.get(Submission, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")

    submission.override_mark = payload.override_mark
    submission.override_feedback = payload.override_feedback
    submission.override_reason = payload.override_reason
    submission.is_flagged = False

    # Write audit log
    log = AuditLog(
        event_type="override",
        submission_id=submission_id,
        detail=f"Mark changed to {payload.override_mark}. Reason: {payload.override_reason or 'N/A'}",
        timestamp=datetime.utcnow(),
    )
    db.add(log)
    await db.commit()
    await db.refresh(submission)
    return submission


@router.get("/flagged")
async def list_flagged(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Submission).where(Submission.is_flagged == True)
    )
    return result.scalars().all()


@router.get("/audit-log")
async def get_audit_log(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AuditLog).order_by(AuditLog.timestamp.desc()))
    logs = result.scalars().all()
    return [
        {
            "id": str(l.id),
            "event_type": l.event_type,
            "actor_id": str(l.actor_id) if l.actor_id else None,
            "submission_id": str(l.submission_id) if l.submission_id else None,
            "detail": l.detail,
            "timestamp": l.timestamp.isoformat(),
        }
        for l in logs
    ]


@router.post("/{submission_id}/retry")
async def retry_marking(
    submission_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    submission = await db.get(Submission, submission_id)
    if not submission:
        raise HTTPException(404, "Submission not found")

    mark_submission_task.delay(str(submission.id))
    return {"status": "queued", "submission_id": str(submission.id)}
