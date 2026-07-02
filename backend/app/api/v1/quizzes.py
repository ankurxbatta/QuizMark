"""
quizzes.py — group questions into a named quiz and assign the quiz to students.

A quiz is the unit of assignment: instructors bundle questions into a titled
quiz and assign it to students. A student's assessment is the union of all
questions in the quizzes assigned to them. Legacy per-question assignment
(questions.assigned_student_ids) still works — a question is answerable if it is
in an assigned quiz OR directly assigned — so nothing built before quizzes
breaks.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_db
from app.core.security import get_current_user, require_instructor
from app.schemas.schemas import (
    QuizAssigneeOut,
    QuizAssigneeUpdate,
    QuizCreate,
    QuizOut,
    QuizUpdate,
    QuizWithQuestions,
)

router = APIRouter()


def _quiz_out(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = doc.pop("_id")
    ids = doc.get("question_ids", []) or []
    doc["question_ids"] = ids
    doc["question_count"] = len(ids)
    doc.setdefault("assigned_student_ids", [])
    doc.setdefault("description", None)
    return doc


def _assessment_q(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = doc.pop("_id")
    doc.setdefault("assets", [])
    doc.setdefault("source_page_range", None)
    doc.setdefault("source_chunk", None)
    doc.pop("embedding", None)
    return doc


async def student_quiz_question_ids(db: AsyncIOMotorDatabase, student_id: str) -> set[str]:
    """All question ids reachable by a student through their assigned quizzes."""
    quizzes = await db["quizzes"].find(
        {"assigned_student_ids": student_id}, {"question_ids": 1}
    ).to_list(length=None)
    ids: set[str] = set()
    for quiz in quizzes:
        ids.update(quiz.get("question_ids", []) or [])
    return ids


async def _validate_question_ids(db: AsyncIOMotorDatabase, question_ids: list[str]) -> list[str]:
    """De-dupe, preserve order, and reject ids that are not real questions."""
    ordered = list(dict.fromkeys(str(q) for q in question_ids))
    if not ordered:
        return []
    found = await db["questions"].find(
        {"_id": {"$in": ordered}}, {"_id": 1}
    ).to_list(length=len(ordered))
    found_ids = {d["_id"] for d in found}
    missing = [q for q in ordered if q not in found_ids]
    if missing:
        raise HTTPException(400, f"Unknown question id(s): {', '.join(missing)}")
    return ordered


# ── Student view ──────────────────────────────────────────────────────────────

@router.get("/mine", response_model=List[QuizWithQuestions])
async def my_quizzes(
    db: AsyncIOMotorDatabase = Depends(get_db),
    claims: dict = Depends(get_current_user),
):
    """Quizzes assigned to the calling student, each with its questions populated."""
    student_id = claims["sub"]
    quizzes = await db["quizzes"].find(
        {"assigned_student_ids": student_id}
    ).sort("created_at", -1).to_list(length=200)

    out: list[dict] = []
    for quiz in quizzes:
        ids = quiz.get("question_ids", []) or []
        questions: list[dict] = []
        if ids:
            docs = await db["questions"].find(
                {"_id": {"$in": ids}}, {"embedding": 0}
            ).to_list(length=len(ids))
            by_id = {d["_id"]: d for d in docs}
            # preserve the quiz's question order
            questions = [_assessment_q(by_id[i]) for i in ids if i in by_id]
        out.append({
            "id": quiz["_id"],
            "title": quiz.get("title", "Quiz"),
            "description": quiz.get("description"),
            "questions": questions,
        })
    return out


# ── Instructor CRUD ───────────────────────────────────────────────────────────

@router.post("/", response_model=QuizOut, status_code=201)
async def create_quiz(
    payload: QuizCreate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    question_ids = await _validate_question_ids(db, payload.question_ids)
    doc = {
        "_id": str(uuid.uuid4()),
        "title": payload.title.strip() or "Untitled quiz",
        "description": (payload.description or "").strip() or None,
        "question_ids": question_ids,
        "assigned_student_ids": [],
        "created_at": datetime.now(timezone.utc),
    }
    await db["quizzes"].insert_one(doc)
    return _quiz_out(doc)


@router.get("/", response_model=List[QuizOut])
async def list_quizzes(
    skip: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=500),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    docs = (
        await db["quizzes"].find({}).sort("created_at", -1)
        .skip(skip).limit(limit).to_list(length=limit)
    )
    return [_quiz_out(d) for d in docs]


@router.get("/{quiz_id}", response_model=QuizOut)
async def get_quiz(
    quiz_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    doc = await db["quizzes"].find_one({"_id": quiz_id})
    if not doc:
        raise HTTPException(404, "Quiz not found")
    return _quiz_out(doc)


@router.put("/{quiz_id}", response_model=QuizOut)
async def update_quiz(
    quiz_id: str,
    payload: QuizUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    doc = await db["quizzes"].find_one({"_id": quiz_id})
    if not doc:
        raise HTTPException(404, "Quiz not found")
    updates: dict = {}
    if payload.title is not None:
        updates["title"] = payload.title.strip() or doc.get("title", "Untitled quiz")
    if payload.description is not None:
        updates["description"] = payload.description.strip() or None
    if payload.question_ids is not None:
        updates["question_ids"] = await _validate_question_ids(db, payload.question_ids)
    if updates:
        await db["quizzes"].update_one({"_id": quiz_id}, {"$set": updates})
    return _quiz_out({**doc, **updates})


@router.delete("/{quiz_id}", status_code=204)
async def delete_quiz(
    quiz_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    res = await db["quizzes"].delete_one({"_id": quiz_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Quiz not found")
    return None


# ── Assignment ────────────────────────────────────────────────────────────────

@router.get("/{quiz_id}/assignees", response_model=QuizAssigneeOut)
async def get_quiz_assignees(
    quiz_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    doc = await db["quizzes"].find_one({"_id": quiz_id}, {"assigned_student_ids": 1})
    if not doc:
        raise HTTPException(404, "Quiz not found")
    return {"quiz_id": quiz_id, "student_ids": doc.get("assigned_student_ids", [])}


@router.put("/{quiz_id}/assignees", response_model=QuizAssigneeOut)
async def update_quiz_assignees(
    quiz_id: str,
    payload: QuizAssigneeUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    if not await db["quizzes"].find_one({"_id": quiz_id}):
        raise HTTPException(404, "Quiz not found")

    student_ids = list(dict.fromkeys(str(s) for s in payload.student_ids))
    if student_ids:
        existing = await db["users"].find(
            {"_id": {"$in": student_ids}, "role": "student"}, {"_id": 1}
        ).to_list(length=len(student_ids))
        found = {d["_id"] for d in existing}
        missing = [s for s in student_ids if s not in found]
        if missing:
            raise HTTPException(400, f"Invalid student id(s): {', '.join(missing)}")

    await db["quizzes"].update_one(
        {"_id": quiz_id}, {"$set": {"assigned_student_ids": student_ids}}
    )
    return {"quiz_id": quiz_id, "student_ids": student_ids}
