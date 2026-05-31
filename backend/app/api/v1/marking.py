import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_db
from app.core.security import require_instructor
from app.schemas.schemas import OverrideRequest, SubmissionOut
from app.tasks.marking_tasks import mark_submission_task

router = APIRouter()


def _sub_out(sub: dict) -> dict:
    out = dict(sub)
    out["id"] = out.pop("_id")
    out.setdefault("question_text", None)
    out.setdefault("question_type", None)
    out.setdefault("max_marks", None)
    return out


@router.put("/{submission_id}/override", response_model=SubmissionOut)
async def override_mark(
    submission_id: str,
    payload: OverrideRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
    claims: dict = Depends(require_instructor),
):
    sub = await db["submissions"].find_one({"_id": submission_id})
    if not sub:
        raise HTTPException(404, "Submission not found")

    await db["submissions"].update_one(
        {"_id": submission_id},
        {"$set": {
            "override_mark": payload.override_mark,
            "override_feedback": payload.override_feedback,
            "override_reason": payload.override_reason,
            "is_flagged": False,
        }},
    )

    actor_id = claims.get("sub", "")
    log_doc = {
        "_id": str(uuid.uuid4()),
        "event_type": "override",
        "actor_id": actor_id,
        "submission_id": submission_id,
        "detail": f"Mark changed to {payload.override_mark}. Reason: {payload.override_reason or 'N/A'}",
        "timestamp": datetime.now(timezone.utc),
    }
    await db["audit_logs"].insert_one(log_doc)

    updated = await db["submissions"].find_one({"_id": submission_id})
    return _sub_out(updated)


@router.get("/flagged")
async def list_flagged(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    subs = await db["submissions"].find({"is_flagged": True}).skip(skip).limit(limit).to_list(length=limit)
    return [_sub_out(s) for s in subs]


@router.get("/audit-log")
async def get_audit_log(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    logs = await db["audit_logs"].find({}).sort("timestamp", -1).skip(skip).limit(limit).to_list(length=limit)
    return [
        {
            "id": str(l["_id"]),
            "event_type": l["event_type"],
            "actor_id": l.get("actor_id"),
            "submission_id": l.get("submission_id"),
            "detail": l.get("detail"),
            "timestamp": l["timestamp"].isoformat(),
        }
        for l in logs
    ]


@router.post("/{submission_id}/retry")
async def retry_marking(
    submission_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    if not await db["submissions"].find_one({"_id": submission_id}):
        raise HTTPException(404, "Submission not found")
    mark_submission_task.delay(submission_id)
    return {"status": "queued", "submission_id": submission_id}
