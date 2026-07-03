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
    return out


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

    existing = await db["submissions"].find_one(
        {"student_id": user_id, "question_id": question_id}
    )
    if existing:
        raise HTTPException(409, "You have already submitted an answer for this question")

    now = datetime.now(timezone.utc)
    sub_doc = {
        "_id": str(uuid.uuid4()),
        "student_id": user_id,
        "question_id": question_id,
        "answer_text": payload.answer_text,
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
        "submitted_at": now,
        "marked_at": None,
    }
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
