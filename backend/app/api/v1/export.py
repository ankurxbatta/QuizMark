import csv
import io
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database import get_db
from app.core.security import require_instructor
from app.models.models import Submission, AuditLog, Question, User

router = APIRouter()


@router.get("/marks")
async def export_marks(db: AsyncSession = Depends(get_db), _: dict = Depends(require_instructor)):
    # Join submissions with questions and users for a useful CSV
    result = await db.execute(
        select(Submission, Question, User)
        .join(Question, Submission.question_id == Question.id)
        .join(User, Submission.student_id == User.id)
    )
    rows = result.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "student_id", "username", "question_id", "question_text",
        "question_type", "mark", "max_mark", "feedback",
        "override_flag", "submitted_at", "marked_at"
    ])
    for s, q, u in rows:
        mark = s.override_mark if s.override_mark is not None else s.auto_mark
        feedback = s.override_feedback or s.auto_feedback or ""
        writer.writerow([
            str(s.student_id),
            u.username,
            str(s.question_id),
            q.question_text[:120],
            q.question_type,
            mark,
            q.max_marks,
            feedback,
            "1" if s.override_mark is not None else "0",
            s.submitted_at.isoformat() if s.submitted_at else "",
            s.marked_at.isoformat() if s.marked_at else "",
        ])

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=marks_export.csv"},
    )


@router.get("/audit")
async def export_audit(db: AsyncSession = Depends(get_db), _: dict = Depends(require_instructor)):
    result = await db.execute(select(AuditLog).order_by(AuditLog.timestamp))
    logs = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "event_type", "actor_id", "submission_id", "detail", "timestamp"])
    for log in logs:
        writer.writerow([
            str(log.id), log.event_type,
            str(log.actor_id) if log.actor_id else "",
            str(log.submission_id) if log.submission_id else "",
            log.detail or "", log.timestamp.isoformat()
        ])

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )
