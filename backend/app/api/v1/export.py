import csv
import io
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database import get_db
from app.models.models import Submission, AuditLog

router = APIRouter()


@router.get("/marks")
async def export_marks(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Submission))
    submissions = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "student_id", "question_id", "mark", "max_mark",
        "feedback", "override_flag", "timestamp"
    ])
    for s in submissions:
        mark = s.override_mark if s.override_mark is not None else s.auto_mark
        feedback = s.override_feedback or s.auto_feedback or ""
        writer.writerow([
            str(s.student_id), str(s.question_id),
            mark, "",  # max_mark requires question join – extend as needed
            feedback,
            "1" if s.override_mark is not None else "0",
            s.marked_at.isoformat() if s.marked_at else "",
        ])

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=marks_export.csv"},
    )


@router.get("/audit")
async def export_audit(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AuditLog).order_by(AuditLog.timestamp))
    logs = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "event_type", "actor_id", "submission_id", "detail", "timestamp"])
    for l in logs:
        writer.writerow([
            str(l.id), l.event_type,
            str(l.actor_id) if l.actor_id else "",
            str(l.submission_id) if l.submission_id else "",
            l.detail or "", l.timestamp.isoformat()
        ])

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )
