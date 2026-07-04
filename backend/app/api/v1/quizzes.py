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
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.core.database import get_db
from app.core.security import get_current_user, require_instructor
from app.schemas.schemas import (
    QuizAssigneeOut,
    QuizAssigneeUpdate,
    QuizAttemptOut,
    QuizAttemptRow,
    QuizAttemptStartOut,
    QuizCreate,
    QuizDraftUpdate,
    QuizOut,
    QuizPlayerState,
    QuizUpdate,
    QuizWithQuestions,
)

router = APIRouter()

# Strict-mode submissions are still accepted this many seconds past the
# deadline so the client's automatic time's-up submit survives slow networks.
STRICT_GRACE_SECONDS = 30


def _quiz_out(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = doc.pop("_id")
    ids = doc.get("question_ids", []) or []
    doc["question_ids"] = ids
    doc["question_count"] = len(ids)
    doc.setdefault("assigned_student_ids", [])
    doc.setdefault("description", None)
    doc.setdefault("time_limit_minutes", None)
    doc.setdefault("timing_mode", "strict")
    return doc


def _player_meta(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "title": doc.get("title", "Quiz"),
        "description": doc.get("description"),
        "question_count": len(doc.get("question_ids", []) or []),
        "time_limit_minutes": doc.get("time_limit_minutes"),
        "timing_mode": doc.get("timing_mode") or "strict",
    }


def _attempt_out(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "quiz_id": doc["quiz_id"],
        "student_id": doc["student_id"],
        "status": doc.get("status", "in_progress"),
        "started_at": doc["started_at"],
        "deadline_at": doc.get("deadline_at"),
        "finished_at": doc.get("finished_at"),
        "duration_seconds": doc.get("duration_seconds"),
        "late_by_seconds": doc.get("late_by_seconds", 0) or 0,
        "draft_answers": doc.get("draft_answers", {}) or {},
    }


def _utc(dt: datetime | None) -> datetime | None:
    """Mongo returns naive UTC datetimes — normalise so comparisons work."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _assessment_q(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = doc.pop("_id")
    doc.setdefault("assets", [])
    doc.setdefault("source_page_range", None)
    doc.setdefault("source_chunk", None)
    doc.pop("embedding", None)
    return doc


async def student_quiz_question_ids(
    db: AsyncIOMotorDatabase, student_id: str, untimed_only: bool = False
) -> set[str]:
    """All question ids reachable by a student through their assigned quizzes.

    untimed_only=True skips timed quizzes — used by the legacy assessment
    listing so timed-quiz questions stay hidden until the student starts the
    quiz in the player (which starts their clock).
    """
    quizzes = await db["quizzes"].find(
        {"assigned_student_ids": student_id}, {"question_ids": 1, "time_limit_minutes": 1}
    ).to_list(length=None)
    ids: set[str] = set()
    for quiz in quizzes:
        if untimed_only and quiz.get("time_limit_minutes"):
            continue
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
        timed = bool(quiz.get("time_limit_minutes"))
        questions: list[dict] = []
        # Timed quizzes never expose questions here — the student must press
        # Start in the quiz player (which starts their clock) to see them.
        if ids and not timed:
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
            "time_limit_minutes": quiz.get("time_limit_minutes"),
            "timing_mode": quiz.get("timing_mode") or "strict",
            "question_count": len(ids),
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
        "time_limit_minutes": payload.time_limit_minutes,
        "timing_mode": payload.timing_mode,
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
    # Distinguish "field omitted" (leave unchanged) from an explicit null
    # (remove the timer) — pydantic tracks this in model_fields_set.
    if "time_limit_minutes" in payload.model_fields_set:
        updates["time_limit_minutes"] = payload.time_limit_minutes
    if payload.timing_mode is not None:
        updates["timing_mode"] = payload.timing_mode
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


# ── Timed attempts / quiz player ─────────────────────────────────────────────
#
# An attempt is one student's run at a quiz: created when they press Start,
# closed when they finish (or when a strict deadline lapses). The player is
# the mobile-first page students reach by scanning the quiz QR code.

async def _get_quiz_for_student(db: AsyncIOMotorDatabase, quiz_id: str, student_id: str) -> dict:
    quiz = await db["quizzes"].find_one({"_id": quiz_id})
    if not quiz:
        raise HTTPException(404, "Quiz not found")
    if student_id not in (quiz.get("assigned_student_ids") or []):
        raise HTTPException(403, "This quiz is not assigned to you")
    return quiz


def _deadline_passed(attempt: dict, now: datetime, grace_seconds: int = 0) -> bool:
    deadline = _utc(attempt.get("deadline_at"))
    return deadline is not None and now > deadline + timedelta(seconds=grace_seconds)


async def _submit_drafts(
    db: AsyncIOMotorDatabase, quiz: dict, attempt: dict, submitted_at: datetime
) -> int:
    """Turn every non-empty draft answer into a real submission (skipping
    questions already submitted) and queue marking. Returns how many were
    created. This is what makes "whatever they filled in" reach the
    instructor even if the student never pressed Submit on a question."""
    from app.api.v1.submissions import build_submission_doc
    from app.tasks.marking_tasks import mark_submission_task

    drafts = attempt.get("draft_answers", {}) or {}
    quiz_qids = quiz.get("question_ids", []) or []
    candidates = {
        qid: text.strip() for qid, text in drafts.items()
        if qid in quiz_qids and isinstance(text, str) and text.strip()
    }
    if not candidates:
        return 0

    existing = await db["submissions"].find(
        {"student_id": attempt["student_id"], "question_id": {"$in": list(candidates)}},
        {"question_id": 1},
    ).to_list(length=len(candidates))
    already = {d["question_id"] for d in existing}

    created = 0
    for qid, text in candidates.items():
        if qid in already:
            continue
        doc = build_submission_doc(
            attempt["student_id"], qid, text,
            quiz_id=quiz["_id"], attempt_id=attempt["_id"],
            late_by_seconds=0, submitted_at=submitted_at,
        )
        try:
            await db["submissions"].insert_one(doc)
        except DuplicateKeyError:
            continue  # student submitted it themselves in a race — theirs wins
        mark_submission_task.delay(doc["_id"])
        created += 1
    return created


async def _finalize_attempt(
    db: AsyncIOMotorDatabase, quiz: dict, attempt: dict, now: datetime, *, expired: bool
) -> dict:
    """Close an attempt: flush drafts to submissions, stamp duration/lateness.

    expired=True is the strict-timeout path — the attempt is cut off at its
    deadline, so duration equals the time limit and nothing counts as late.
    """
    deadline = _utc(attempt.get("deadline_at"))
    started = _utc(attempt["started_at"])
    finished = deadline if (expired and deadline) else now
    late = 0
    if not expired and deadline and now > deadline:
        late = int((now - deadline).total_seconds())

    submitted_at = min(finished, now)
    await _submit_drafts(db, quiz, attempt, submitted_at)

    updates = {
        "status": "expired" if expired else "completed",
        "finished_at": finished,
        "duration_seconds": max(0, int((finished - started).total_seconds())),
        "late_by_seconds": late,
    }
    # Only the first finalization wins — a lazy expiry sweep racing the
    # student's own Finish must not overwrite the recorded duration.
    res = await db["quiz_attempts"].update_one(
        {"_id": attempt["_id"], "status": "in_progress"}, {"$set": updates}
    )
    if res.modified_count == 0:
        fresh = await db["quiz_attempts"].find_one({"_id": attempt["_id"]})
        return fresh or {**attempt, **updates}
    return {**attempt, **updates}


async def _sweep_expired(
    db: AsyncIOMotorDatabase, quiz: dict, attempt: dict | None, now: datetime
) -> dict | None:
    """Lazily finalize a strict attempt whose deadline (plus grace) lapsed while
    the student was away, so their drafts still reach the instructor."""
    if (
        attempt
        and attempt.get("status") == "in_progress"
        and quiz.get("time_limit_minutes")
        and (quiz.get("timing_mode") or "strict") == "strict"
        and _deadline_passed(attempt, now, STRICT_GRACE_SECONDS)
    ):
        return await _finalize_attempt(db, quiz, attempt, now, expired=True)
    return attempt


@router.get("/{quiz_id}/player", response_model=QuizPlayerState)
async def quiz_player_state(
    quiz_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    claims: dict = Depends(get_current_user),
):
    """Lobby state for the quiz player: quiz metadata plus the caller's attempt
    (if any). Questions are deliberately not included — Start reveals them."""
    student_id = claims["sub"]
    quiz = await _get_quiz_for_student(db, quiz_id, student_id)
    now = datetime.now(timezone.utc)
    attempt = await db["quiz_attempts"].find_one({"quiz_id": quiz_id, "student_id": student_id})
    attempt = await _sweep_expired(db, quiz, attempt, now)
    return {
        "quiz": _player_meta(quiz),
        "attempt": _attempt_out(attempt) if attempt else None,
        "server_now": now,
    }


@router.post("/{quiz_id}/attempt/start", response_model=QuizAttemptStartOut)
async def start_attempt(
    quiz_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    claims: dict = Depends(get_current_user),
):
    """Start (or resume) the calling student's attempt and reveal the questions.

    Idempotent: pressing Start twice, refreshing mid-quiz, or reopening the
    page returns the same attempt with its original deadline, saved drafts,
    and already-submitted answers.
    """
    student_id = claims["sub"]
    quiz = await _get_quiz_for_student(db, quiz_id, student_id)
    now = datetime.now(timezone.utc)

    attempt = await db["quiz_attempts"].find_one({"quiz_id": quiz_id, "student_id": student_id})
    if attempt is None:
        limit = quiz.get("time_limit_minutes")
        doc = {
            "_id": str(uuid.uuid4()),
            "quiz_id": quiz_id,
            "student_id": student_id,
            "status": "in_progress",
            "started_at": now,
            "deadline_at": now + timedelta(minutes=limit) if limit else None,
            "finished_at": None,
            "duration_seconds": None,
            "late_by_seconds": 0,
            "draft_answers": {},
        }
        try:
            await db["quiz_attempts"].insert_one(doc)
            attempt = doc
        except DuplicateKeyError:
            # double-tap on Start raced the unique (quiz_id, student_id) index
            attempt = await db["quiz_attempts"].find_one(
                {"quiz_id": quiz_id, "student_id": student_id}
            )
    attempt = await _sweep_expired(db, quiz, attempt, now)

    qids = quiz.get("question_ids", []) or []
    questions: list[dict] = []
    if qids:
        docs = await db["questions"].find(
            {"_id": {"$in": qids}}, {"embedding": 0}
        ).to_list(length=len(qids))
        by_id = {d["_id"]: d for d in docs}
        questions = [_assessment_q(by_id[i]) for i in qids if i in by_id]

    subs = await db["submissions"].find(
        {"student_id": student_id, "question_id": {"$in": qids}},
        {"question_id": 1, "answer_text": 1, "is_marked": 1},
    ).to_list(length=len(qids)) if qids else []
    submitted = {
        s["question_id"]: {
            "submission_id": s["_id"],
            "answer_text": s.get("answer_text", ""),
            "is_marked": bool(s.get("is_marked")),
        }
        for s in subs
    }

    return {
        "quiz": _player_meta(quiz),
        "attempt": _attempt_out(attempt),
        "questions": questions,
        "submitted": submitted,
        "server_now": now,
    }


@router.put("/{quiz_id}/attempt/draft", response_model=QuizAttemptOut)
async def save_draft(
    quiz_id: str,
    payload: QuizDraftUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    claims: dict = Depends(get_current_user),
):
    """Autosave draft answers (partial: only changed keys need to be sent).
    Drafts survive reloads and device switches, and are what gets flushed to
    the instructor if time runs out."""
    student_id = claims["sub"]
    quiz = await _get_quiz_for_student(db, quiz_id, student_id)
    now = datetime.now(timezone.utc)

    attempt = await db["quiz_attempts"].find_one({"quiz_id": quiz_id, "student_id": student_id})
    if attempt is None:
        raise HTTPException(404, "Start the quiz before saving answers")
    attempt = await _sweep_expired(db, quiz, attempt, now)
    if attempt.get("status") != "in_progress":
        raise HTTPException(409, "This attempt is already finished")

    quiz_qids = set(quiz.get("question_ids", []) or [])
    updates = {
        f"draft_answers.{qid}": text[:20000]
        for qid, text in payload.answers.items()
        if qid in quiz_qids and isinstance(text, str)
    }
    if updates:
        await db["quiz_attempts"].update_one({"_id": attempt["_id"]}, {"$set": updates})
        merged = dict(attempt.get("draft_answers", {}) or {})
        for key, value in updates.items():
            merged[key.split(".", 1)[1]] = value
        attempt["draft_answers"] = merged
    return _attempt_out(attempt)


@router.post("/{quiz_id}/attempt/finish", response_model=QuizAttemptOut)
async def finish_attempt(
    quiz_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    claims: dict = Depends(get_current_user),
):
    """Finish the attempt: any unsubmitted non-empty drafts are submitted for
    marking, and the time taken is recorded for the instructor. Idempotent."""
    student_id = claims["sub"]
    quiz = await _get_quiz_for_student(db, quiz_id, student_id)
    now = datetime.now(timezone.utc)

    attempt = await db["quiz_attempts"].find_one({"quiz_id": quiz_id, "student_id": student_id})
    if attempt is None:
        raise HTTPException(404, "Start the quiz before finishing it")
    if attempt.get("status") != "in_progress":
        return _attempt_out(attempt)

    strict = (quiz.get("timing_mode") or "strict") == "strict"
    expired = bool(
        quiz.get("time_limit_minutes")
        and strict
        and _deadline_passed(attempt, now, STRICT_GRACE_SECONDS)
    )
    attempt = await _finalize_attempt(db, quiz, attempt, now, expired=expired)
    return _attempt_out(attempt)


@router.get("/{quiz_id}/attempts", response_model=List[QuizAttemptRow])
async def list_attempts(
    quiz_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    """Instructor view: every student's attempt with time taken, lateness and
    marking progress. Also lazily finalizes strict attempts whose deadline
    passed while the student was offline, so their drafts get marked."""
    quiz = await db["quizzes"].find_one({"_id": quiz_id})
    if not quiz:
        raise HTTPException(404, "Quiz not found")
    now = datetime.now(timezone.utc)

    attempts = await db["quiz_attempts"].find({"quiz_id": quiz_id}).sort(
        "started_at", 1
    ).to_list(length=None)
    attempts = [await _sweep_expired(db, quiz, a, now) for a in attempts]

    student_ids = [a["student_id"] for a in attempts]
    users = await db["users"].find(
        {"_id": {"$in": student_ids}}, {"username": 1}
    ).to_list(length=len(student_ids)) if student_ids else []
    names = {u["_id"]: u.get("username", "?") for u in users}

    qids = quiz.get("question_ids", []) or []
    q_docs = await db["questions"].find(
        {"_id": {"$in": qids}}, {"max_marks": 1}
    ).to_list(length=len(qids)) if qids else []
    max_total = sum(float(d.get("max_marks") or 0) for d in q_docs)

    subs = await db["submissions"].find(
        {"student_id": {"$in": student_ids}, "question_id": {"$in": qids}},
        {"student_id": 1, "question_id": 1, "is_marked": 1, "auto_mark": 1, "override_mark": 1},
    ).to_list(length=None) if (student_ids and qids) else []
    by_student: dict[str, list[dict]] = {}
    for s in subs:
        by_student.setdefault(s["student_id"], []).append(s)

    rows: list[dict] = []
    for a in attempts:
        mine = by_student.get(a["student_id"], [])
        marked = [s for s in mine if s.get("is_marked")]
        score = sum(
            float(s["override_mark"] if s.get("override_mark") is not None else (s.get("auto_mark") or 0))
            for s in marked
        )
        rows.append({
            "attempt_id": a["_id"],
            "student_id": a["student_id"],
            "username": names.get(a["student_id"], a["student_id"]),
            "status": a.get("status", "in_progress"),
            "started_at": a["started_at"],
            "deadline_at": a.get("deadline_at"),
            "finished_at": a.get("finished_at"),
            "duration_seconds": a.get("duration_seconds"),
            "late_by_seconds": a.get("late_by_seconds", 0) or 0,
            "answered_count": len(mine),
            "marked_count": len(marked),
            "total_questions": len(qids),
            "score": score if marked else None,
            "max_score": max_total or None,
        })
    return rows
