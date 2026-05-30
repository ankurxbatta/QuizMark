import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_db
from app.core.security import require_instructor

router = APIRouter()


@router.get("/marks")
async def export_marks(
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    subs = await db["submissions"].find({}).to_list(length=50000)

    # Batch-fetch questions and users to avoid N+1 queries
    q_ids = list({s["question_id"] for s in subs})
    u_ids = list({s["student_id"] for s in subs})

    questions = {d["_id"]: d for d in await db["questions"].find({"_id": {"$in": q_ids}}, {"embedding": 0}).to_list(length=len(q_ids))}
    users = {d["_id"]: d for d in await db["users"].find({"_id": {"$in": u_ids}}).to_list(length=len(u_ids))}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "student_id", "username", "question_id", "question_text",
        "question_type", "mark", "max_mark", "feedback",
        "override_flag", "submitted_at", "marked_at",
    ])
    for s in subs:
        q = questions.get(s["question_id"], {})
        u = users.get(s["student_id"], {})
        mark = s.get("override_mark") if s.get("override_mark") is not None else s.get("auto_mark")
        feedback = s.get("override_feedback") or s.get("auto_feedback") or ""
        writer.writerow([
            s["student_id"],
            u.get("username", ""),
            s["question_id"],
            (q.get("question_text") or "")[:120],
            q.get("question_type", ""),
            mark,
            q.get("max_marks", ""),
            feedback,
            "1" if s.get("override_mark") is not None else "0",
            s["submitted_at"].isoformat() if s.get("submitted_at") else "",
            s["marked_at"].isoformat() if s.get("marked_at") else "",
        ])

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=marks_export.csv"},
    )


@router.get("/audit")
async def export_audit(
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    logs = await db["audit_logs"].find({}).sort("timestamp", 1).to_list(length=50000)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "event_type", "actor_id", "submission_id", "detail", "timestamp"])
    for log in logs:
        writer.writerow([
            str(log["_id"]),
            log["event_type"],
            log.get("actor_id", ""),
            log.get("submission_id", ""),
            log.get("detail", ""),
            log["timestamp"].isoformat() if log.get("timestamp") else "",
        ])

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )
