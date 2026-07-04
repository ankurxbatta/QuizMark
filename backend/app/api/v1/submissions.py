import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.core.database import get_db
from app.core.security import get_current_user, require_instructor
from app.schemas.schemas import SubmissionCreate, SubmissionOut
from app.tasks.marking_tasks import mark_submission_task

router = APIRouter()


def _sub_out(sub: dict, question: dict | None = None) -> dict:
    out = dict(sub)
    out["id"] = out.pop("_id")
    if question:
        out["question_text"] = question.get("question_text")
        out["question_type"] = question.get("question_type")
        out["max_marks"] = question.get("max_marks")
    else:
        out.setdefault("question_text", None)
        out.setdefault("question_type", None)
        out.setdefault("max_marks", None)
    out.setdefault("quiz_id", None)
    out.setdefault("late_by_seconds", 0)
    return out


def build_submission_doc(
    student_id: str,
    question_id: str,
    answer_text: str,
    *,
    quiz_id: str | None = None,
    attempt_id: str | None = None,
    late_by_seconds: int = 0,
    submitted_at: datetime | None = None,
) -> dict:
    """Canonical unmarked-submission document. Shared with the quiz-attempt
    finalizer (quizzes.py) which flushes draft answers when time runs out."""
    return {
        "_id": str(uuid.uuid4()),
        "student_id": student_id,
        "question_id": question_id,
        "answer_text": answer_text,
        "quiz_id": quiz_id,
        "attempt_id": attempt_id,
        "late_by_seconds": late_by_seconds,
        "auto_mark": None,
        "auto_feedback": None,
        "auto_confidence": None,
        "marking_route": None,
        # legacy "slm_*" key names — see services/pre_scorer.py
        "slm_keyword_coverage": None,
        "slm_semantic_sim": None,
        "slm_raw_score": None,
        "override_mark": None,
        "override_feedback": None,
        "override_reason": None,
        "is_flagged": False,
        "is_marked": False,
        "submitted_at": submitted_at or datetime.now(timezone.utc),
        "marked_at": None,
    }


@router.get("/my", response_model=List[SubmissionOut])
async def list_my_submissions(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncIOMotorDatabase = Depends(get_db),
    claims: dict = Depends(get_current_user),
):
    student_id = claims["sub"]
    subs = await db["submissions"].find({"student_id": student_id}).sort("submitted_at", 1).skip(skip).limit(limit).to_list(length=limit)
    result = []
    for sub in subs:
        q = await db["questions"].find_one({"_id": sub["question_id"]}, {"question_text": 1, "question_type": 1, "max_marks": 1})
        result.append(_sub_out(sub, q))
    return result


async def _enforce_quiz_timing(
    db: AsyncIOMotorDatabase,
    student_id: str,
    question_id: str,
    explicit_quiz_id: str | None,
) -> tuple[str | None, str | None, int]:
    """Apply timed-quiz rules to a submission. Returns (quiz_id, attempt_id,
    late_by_seconds) to stamp on the submission document.

    With an explicit quiz_id (the quiz player always sends one) the quiz is
    validated and, if timed, its attempt deadline is enforced. Without one,
    a question reachable through any untimed quiz (or legacy direct
    assignment) is unrestricted — but a question reachable ONLY through
    timed quizzes requires a started attempt, so the timer cannot be
    bypassed by submitting from the plain desktop form.
    """
    from app.api.v1.quizzes import STRICT_GRACE_SECONDS, _utc

    now = datetime.now(timezone.utc)

    if explicit_quiz_id:
        quiz = await db["quizzes"].find_one({"_id": explicit_quiz_id})
        if not quiz or question_id not in (quiz.get("question_ids") or []):
            raise HTTPException(400, "This question is not part of the given quiz")
        if student_id not in (quiz.get("assigned_student_ids") or []):
            raise HTTPException(403, "This quiz is not assigned to you")
        if not quiz.get("time_limit_minutes"):
            return quiz["_id"], None, 0
        candidates = [quiz]
    else:
        candidates = await db["quizzes"].find(
            {"assigned_student_ids": student_id, "question_ids": question_id},
            {"question_ids": 0},
        ).to_list(length=None)
        if not candidates or any(not q.get("time_limit_minutes") for q in candidates):
            # Reachable without a timer — legacy behaviour, nothing to enforce.
            quiz_id = candidates[0]["_id"] if len(candidates) == 1 else None
            return quiz_id, None, 0

    attempts = await db["quiz_attempts"].find(
        {"student_id": student_id, "quiz_id": {"$in": [q["_id"] for q in candidates]}}
    ).to_list(length=len(candidates))
    by_quiz = {a["quiz_id"]: a for a in attempts}
    pair = next(
        ((q, by_quiz[q["_id"]]) for q in candidates
         if by_quiz.get(q["_id"], {}).get("status") == "in_progress"),
        None,
    )
    if pair is None:
        if attempts:
            raise HTTPException(409, "Your attempt at this quiz is already finished")
        raise HTTPException(403, "This is a timed quiz — press Start in the quiz to begin")

    quiz, attempt = pair
    deadline = _utc(attempt.get("deadline_at"))
    late = int((now - deadline).total_seconds()) if deadline and now > deadline else 0
    strict = (quiz.get("timing_mode") or "strict") == "strict"
    if strict and late > STRICT_GRACE_SECONDS:
        raise HTTPException(410, "Time is up — this quiz no longer accepts answers")
    return quiz["_id"], attempt["_id"], late


@router.post("/", response_model=SubmissionOut, status_code=201)
async def submit_answer(
    payload: SubmissionCreate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    claims: dict = Depends(get_current_user),
):
    user_id = claims["sub"]
    user = await db["users"].find_one({"_id": user_id})
    if not user:
        raise HTTPException(401, "User not found")
    if user["role"] != "student":
        raise HTTPException(403, "Student account required to submit answers")

    question_id = str(payload.question_id)
    question = await db["questions"].find_one({"_id": question_id})
    if not question:
        raise HTTPException(404, "Question not found")

    from app.api.v1.quizzes import student_quiz_question_ids
    via_quiz = question_id in await student_quiz_question_ids(db, user_id)
    if not via_quiz and user_id not in question.get("assigned_student_ids", []):
        raise HTTPException(403, "This question is not assigned to you")

    quiz_id, attempt_id, late_by_seconds = await _enforce_quiz_timing(
        db, user_id, question_id, payload.quiz_id
    )

    existing = await db["submissions"].find_one(
        {"student_id": user_id, "question_id": question_id}
    )
    if existing:
        raise HTTPException(409, "You have already submitted an answer for this question")

    sub_doc = build_submission_doc(
        user_id, question_id, payload.answer_text,
        quiz_id=quiz_id, attempt_id=attempt_id, late_by_seconds=late_by_seconds,
    )
    try:
        await db["submissions"].insert_one(sub_doc)
    except DuplicateKeyError:
        # Concurrent double-submit lost the race against the UNIQUE
        # (student_id, question_id) index — return the same clean 409 as the
        # fast-path pre-check so exactly one submission is ever created.
        raise HTTPException(409, "You have already submitted an answer for this question")
    mark_submission_task.delay(sub_doc["_id"])
    return _sub_out(sub_doc, question)


@router.get("/", response_model=List[SubmissionOut])
async def list_submissions(
    flagged_only: bool = False,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    filt = {"is_flagged": True} if flagged_only else {}
    subs = await db["submissions"].find(filt).skip(skip).limit(limit).to_list(length=limit)
    result = []
    for sub in subs:
        q = await db["questions"].find_one({"_id": sub["question_id"]}, {"question_text": 1, "question_type": 1, "max_marks": 1})
        result.append(_sub_out(sub, q))
    return result


@router.get("/{submission_id}", response_model=SubmissionOut)
async def get_submission(
    submission_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    sub = await db["submissions"].find_one({"_id": submission_id})
    if not sub:
        raise HTTPException(404, "Submission not found")
    q = await db["questions"].find_one({"_id": sub["question_id"]}, {"question_text": 1, "question_type": 1, "max_marks": 1})
    return _sub_out(sub, q)
