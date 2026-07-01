import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import StreamingResponse
from jose import JWTError
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_db
from app.core.security import decode_token

router = APIRouter()

QUESTION_BATCH_SIZE = 500


def require_instructor_with_token(
    token: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
) -> dict:
    """
    Auth for browser-initiated CSV downloads.

    An `<a href download>` cannot attach an Authorization header, so we accept
    the JWT either from the normal `Authorization: Bearer` header OR, as a
    fallback, from a `?token=` query param (same pattern as the SSE
    /jobs/{id}/stream and /assets/{id} endpoints). Instructor role is required.
    """
    raw = None
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    if not raw:
        raw = token
    if not raw:
        raise HTTPException(401, "Authentication required")
    try:
        claims = decode_token(raw)
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")
    if not claims.get("sub"):
        raise HTTPException(401, "Invalid or expired token")
    if claims.get("role") != "instructor":
        raise HTTPException(403, "Instructor access required")
    return claims


def safe_csv_value(value):
    """Neutralise CSV formula injection: prefix =, +, -, @ with an apostrophe."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _csv_row(values: list) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerow([safe_csv_value(v) for v in values])
    return buf.getvalue()


@router.get("/marks")
async def export_marks(
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor_with_token),
):
    async def generate():
        yield _csv_row([
            "student_id", "username", "question_id", "question_text",
            "question_type", "mark", "max_mark", "feedback",
            "override_flag", "submitted_at", "marked_at",
        ])

        # Users are few — prefetch them once.
        users = {
            d["_id"]: d
            async for d in db["users"].find({}, {"username": 1})
        }

        questions: dict[str, dict] = {}

        async def ensure_questions(q_ids: set[str]):
            missing = [qid for qid in q_ids if qid not in questions]
            for i in range(0, len(missing), QUESTION_BATCH_SIZE):
                batch = missing[i:i + QUESTION_BATCH_SIZE]
                async for q in db["questions"].find(
                    {"_id": {"$in": batch}},
                    {"question_text": 1, "question_type": 1, "max_marks": 1},
                ):
                    questions[q["_id"]] = q

        buffer: list[dict] = []

        async def flush(subs: list[dict]):
            await ensure_questions({s["question_id"] for s in subs})
            for s in subs:
                q = questions.get(s["question_id"], {})
                u = users.get(s["student_id"], {})
                mark = s.get("override_mark") if s.get("override_mark") is not None else s.get("auto_mark")
                feedback = s.get("override_feedback") or s.get("auto_feedback") or ""
                yield _csv_row([
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

        async for sub in db["submissions"].find({}):
            buffer.append(sub)
            if len(buffer) >= QUESTION_BATCH_SIZE:
                async for row in flush(buffer):
                    yield row
                buffer = []

        if buffer:
            async for row in flush(buffer):
                yield row

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=marks_export.csv"},
    )


@router.get("/audit")
async def export_audit(
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor_with_token),
):
    async def generate():
        yield _csv_row(["id", "event_type", "actor_id", "submission_id", "detail", "timestamp"])
        async for log in db["audit_logs"].find({}).sort("timestamp", 1):
            yield _csv_row([
                str(log["_id"]),
                log["event_type"],
                log.get("actor_id", ""),
                log.get("submission_id", ""),
                log.get("detail", ""),
                log["timestamp"].isoformat() if log.get("timestamp") else "",
            ])

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )
