"""
Timed quiz attempts: the quiz player lifecycle (start → draft → submit →
finish), strict/easy deadline enforcement on submissions, lazy expiry that
flushes drafts to the instructor, and the instructor attempts view.

Celery is never hit: mark_submission_task.delay is monkeypatched (the task
object is shared, so patching it covers both submissions.py and quizzes.py).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.api.v1 import submissions as submissions_mod


def _bearer(token_factory, role="student", sub="stud-1"):
    return {"Authorization": f"Bearer {token_factory(role, sub)}"}


async def _seed_student(db, sid="stud-1", username="stud1"):
    await db["users"].insert_one({"_id": sid, "username": username, "role": "student"})


async def _seed_question(db, qid="q1", text="What is 2+2?", max_marks=2):
    await db["questions"].insert_one({
        "_id": qid, "question_text": text, "question_type": "short_answer",
        "model_answer": "4", "rubric": "", "max_marks": max_marks,
        "assigned_student_ids": [], "created_at": datetime.now(timezone.utc),
    })


async def _seed_quiz(db, quiz_id="quiz-1", qids=("q1", "q2"), students=("stud-1",),
                     limit=10, mode="strict"):
    await db["quizzes"].insert_one({
        "_id": quiz_id, "title": "Timed", "description": None,
        "question_ids": list(qids), "assigned_student_ids": list(students),
        "time_limit_minutes": limit, "timing_mode": mode,
        "created_at": datetime.now(timezone.utc),
    })


@pytest.fixture()
def fake_delay(monkeypatch):
    m = MagicMock()
    monkeypatch.setattr(submissions_mod.mark_submission_task, "delay", m)
    return m


async def _expire_attempt(db, quiz_id="quiz-1", sid="user-1", minutes_ago=5):
    """Move an attempt's deadline into the past (beyond the grace window)."""
    await db["quiz_attempts"].update_one(
        {"quiz_id": quiz_id, "student_id": sid},
        {"$set": {"deadline_at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)}},
    )


# ── Quiz CRUD carries the timer fields ────────────────────────────────────────

@pytest.mark.asyncio
async def test_quiz_create_and_update_timer_fields(client, token_factory, mock_db):
    h = _bearer(token_factory, "instructor")
    r = client.post("/api/v1/quizzes/", json={
        "title": "T", "time_limit_minutes": 15, "timing_mode": "easy",
    }, headers=h)
    assert r.status_code == 201, r.text
    quiz = r.json()
    assert quiz["time_limit_minutes"] == 15
    assert quiz["timing_mode"] == "easy"

    # explicit null removes the timer; omitting the field leaves it unchanged
    r = client.put(f"/api/v1/quizzes/{quiz['id']}", json={"timing_mode": "strict"}, headers=h)
    assert r.json()["time_limit_minutes"] == 15
    assert r.json()["timing_mode"] == "strict"
    r = client.put(f"/api/v1/quizzes/{quiz['id']}", json={"time_limit_minutes": None}, headers=h)
    assert r.json()["time_limit_minutes"] is None


# ── Player lobby + start ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_player_state_and_start_lifecycle(client, token_factory, mock_db):
    await _seed_student(mock_db)
    await _seed_question(mock_db, "q1")
    await _seed_question(mock_db, "q2", "What is 3+3?")
    await _seed_quiz(mock_db, limit=10)
    sh = _bearer(token_factory)

    # lobby: no attempt yet, questions are NOT exposed
    r = client.get("/api/v1/quizzes/quiz-1/player", headers=sh)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["attempt"] is None
    assert body["quiz"]["question_count"] == 2
    assert "questions" not in body

    # start: attempt created with a deadline 10 minutes out
    r = client.post("/api/v1/quizzes/quiz-1/attempt/start", headers=sh)
    assert r.status_code == 200, r.text
    started = r.json()
    assert [q["id"] for q in started["questions"]] == ["q1", "q2"]
    attempt = started["attempt"]
    assert attempt["status"] == "in_progress"
    dl = datetime.fromisoformat(attempt["deadline_at"])
    st = datetime.fromisoformat(attempt["started_at"])
    assert abs((dl - st).total_seconds() - 600) < 2

    # start again: same attempt (idempotent resume), not a fresh clock
    r2 = client.post("/api/v1/quizzes/quiz-1/attempt/start", headers=sh)
    assert r2.json()["attempt"]["id"] == attempt["id"]
    dl2 = datetime.fromisoformat(r2.json()["attempt"]["deadline_at"].replace("Z", "+00:00"))
    if dl2.tzinfo is None:
        dl2 = dl2.replace(tzinfo=timezone.utc)
    if dl.tzinfo is None:
        dl = dl.replace(tzinfo=timezone.utc)
    assert abs((dl2 - dl).total_seconds()) < 1


@pytest.mark.asyncio
async def test_player_forbidden_when_not_assigned(client, token_factory, mock_db):
    await _seed_student(mock_db, "other", "other")
    await _seed_question(mock_db, "q1")
    await _seed_quiz(mock_db, students=("stud-1",))
    r = client.get("/api/v1/quizzes/quiz-1/player", headers=_bearer(token_factory, sub="other"))
    assert r.status_code == 403


# ── Drafts ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_draft_saved_and_returned_on_resume(client, token_factory, mock_db):
    await _seed_student(mock_db)
    await _seed_question(mock_db, "q1")
    await _seed_question(mock_db, "q2")
    await _seed_quiz(mock_db)
    sh = _bearer(token_factory)
    client.post("/api/v1/quizzes/quiz-1/attempt/start", headers=sh)

    r = client.put("/api/v1/quizzes/quiz-1/attempt/draft",
                   json={"answers": {"q1": "draft one", "bogus": "ignored"}}, headers=sh)
    assert r.status_code == 200, r.text
    assert r.json()["draft_answers"] == {"q1": "draft one"}

    # partial update merges rather than replaces
    client.put("/api/v1/quizzes/quiz-1/attempt/draft",
               json={"answers": {"q2": "draft two"}}, headers=sh)
    resumed = client.post("/api/v1/quizzes/quiz-1/attempt/start", headers=sh).json()
    assert resumed["attempt"]["draft_answers"] == {"q1": "draft one", "q2": "draft two"}


# ── Timed questions are hidden from the untimed surfaces ────────────────────

@pytest.mark.asyncio
async def test_timed_quiz_questions_hidden_until_started(client, token_factory, mock_db):
    await _seed_student(mock_db)
    await _seed_question(mock_db, "q1")
    await _seed_quiz(mock_db, qids=("q1",))
    sh = _bearer(token_factory)

    mine = client.get("/api/v1/quizzes/mine", headers=sh).json()
    assert mine[0]["time_limit_minutes"] == 10
    assert mine[0]["question_count"] == 1
    assert mine[0]["questions"] == []

    assessment = client.get("/api/v1/questions/assessment", headers=sh).json()
    assert assessment == []


# ── Submission enforcement ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_requires_started_attempt_for_timed_quiz(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db, "user-1", "stud")
    await _seed_question(mock_db, "q1")
    await _seed_quiz(mock_db, qids=("q1",), students=("user-1",))
    r = client.post("/api/v1/submissions/", json={"question_id": "q1", "answer_text": "4"},
                    headers=_bearer(token_factory, sub="user-1"))
    assert r.status_code == 403
    assert "timed" in r.json()["detail"].lower()
    fake_delay.assert_not_called()


@pytest.mark.asyncio
async def test_submit_within_deadline_records_quiz_and_no_lateness(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db, "user-1", "stud")
    await _seed_question(mock_db, "q1")
    await _seed_quiz(mock_db, qids=("q1",), students=("user-1",))
    sh = _bearer(token_factory, sub="user-1")
    client.post("/api/v1/quizzes/quiz-1/attempt/start", headers=sh)

    r = client.post("/api/v1/submissions/",
                    json={"question_id": "q1", "answer_text": "4", "quiz_id": "quiz-1"},
                    headers=sh)
    assert r.status_code == 201, r.text
    assert r.json()["quiz_id"] == "quiz-1"
    assert r.json()["late_by_seconds"] == 0
    fake_delay.assert_called_once()


@pytest.mark.asyncio
async def test_strict_submit_rejected_after_deadline(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db, "user-1", "stud")
    await _seed_question(mock_db, "q1")
    await _seed_quiz(mock_db, qids=("q1",), students=("user-1",), mode="strict")
    sh = _bearer(token_factory, sub="user-1")
    client.post("/api/v1/quizzes/quiz-1/attempt/start", headers=sh)
    await _expire_attempt(mock_db)

    r = client.post("/api/v1/submissions/",
                    json={"question_id": "q1", "answer_text": "4", "quiz_id": "quiz-1"},
                    headers=sh)
    assert r.status_code == 410
    fake_delay.assert_not_called()


@pytest.mark.asyncio
async def test_easy_submit_accepted_late_with_lateness_recorded(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db, "user-1", "stud")
    await _seed_question(mock_db, "q1")
    await _seed_quiz(mock_db, qids=("q1",), students=("user-1",), mode="easy")
    sh = _bearer(token_factory, sub="user-1")
    client.post("/api/v1/quizzes/quiz-1/attempt/start", headers=sh)
    await _expire_attempt(mock_db, minutes_ago=5)

    r = client.post("/api/v1/submissions/",
                    json={"question_id": "q1", "answer_text": "4", "quiz_id": "quiz-1"},
                    headers=sh)
    assert r.status_code == 201, r.text
    assert r.json()["late_by_seconds"] >= 290  # ~5 minutes late
    fake_delay.assert_called_once()


@pytest.mark.asyncio
async def test_untimed_quiz_submission_still_works(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db, "user-1", "stud")
    await _seed_question(mock_db, "q1")
    await _seed_quiz(mock_db, qids=("q1",), students=("user-1",), limit=None)
    r = client.post("/api/v1/submissions/", json={"question_id": "q1", "answer_text": "4"},
                    headers=_bearer(token_factory, sub="user-1"))
    assert r.status_code == 201, r.text
    fake_delay.assert_called_once()


# ── Finish + expiry flush drafts ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_finish_flushes_drafts_and_records_duration(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db, "user-1", "stud")
    await _seed_question(mock_db, "q1")
    await _seed_question(mock_db, "q2")
    await _seed_quiz(mock_db, students=("user-1",))
    sh = _bearer(token_factory, sub="user-1")
    client.post("/api/v1/quizzes/quiz-1/attempt/start", headers=sh)

    # q1 submitted directly; q2 left as a draft
    client.post("/api/v1/submissions/",
                json={"question_id": "q1", "answer_text": "four", "quiz_id": "quiz-1"}, headers=sh)
    client.put("/api/v1/quizzes/quiz-1/attempt/draft",
               json={"answers": {"q2": "six-ish", "q1": "stale draft"}}, headers=sh)

    r = client.post("/api/v1/quizzes/quiz-1/attempt/finish", headers=sh)
    assert r.status_code == 200, r.text
    finished = r.json()
    assert finished["status"] == "completed"
    assert finished["duration_seconds"] is not None and finished["duration_seconds"] >= 0

    # the q2 draft became a real submission; q1 was NOT overwritten by its stale draft
    subs = await mock_db["submissions"].find({"student_id": "user-1"}).to_list(None)
    by_q = {s["question_id"]: s for s in subs}
    assert set(by_q) == {"q1", "q2"}
    assert by_q["q1"]["answer_text"] == "four"
    assert by_q["q2"]["answer_text"] == "six-ish"
    assert by_q["q2"]["attempt_id"] == finished["id"]
    assert fake_delay.call_count == 2

    # idempotent: finishing again changes nothing
    again = client.post("/api/v1/quizzes/quiz-1/attempt/finish", headers=sh).json()
    assert again["duration_seconds"] == finished["duration_seconds"]


@pytest.mark.asyncio
async def test_expired_strict_attempt_lazily_finalized_with_drafts(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db, "user-1", "stud")
    await _seed_question(mock_db, "q1")
    await _seed_quiz(mock_db, qids=("q1",), students=("user-1",), mode="strict", limit=10)
    sh = _bearer(token_factory, sub="user-1")
    client.post("/api/v1/quizzes/quiz-1/attempt/start", headers=sh)
    client.put("/api/v1/quizzes/quiz-1/attempt/draft",
               json={"answers": {"q1": "written before cutoff"}}, headers=sh)
    await _expire_attempt(mock_db)

    # any later touch (here: the lobby) finalizes the attempt as expired
    r = client.get("/api/v1/quizzes/quiz-1/player", headers=sh)
    assert r.json()["attempt"]["status"] == "expired"
    assert r.json()["attempt"]["duration_seconds"] is not None

    subs = await mock_db["submissions"].find({"student_id": "user-1"}).to_list(None)
    assert len(subs) == 1 and subs[0]["answer_text"] == "written before cutoff"
    fake_delay.assert_called_once()

    # drafts can no longer be saved on the dead attempt
    r = client.put("/api/v1/quizzes/quiz-1/attempt/draft",
                   json={"answers": {"q1": "too late"}}, headers=sh)
    assert r.status_code == 409


# ── Instructor attempts view ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_instructor_attempts_view(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db, "user-1", "alice")
    await _seed_question(mock_db, "q1", max_marks=2)
    await _seed_question(mock_db, "q2", max_marks=3)
    await _seed_quiz(mock_db, students=("user-1",), mode="easy")
    sh = _bearer(token_factory, sub="user-1")
    client.post("/api/v1/quizzes/quiz-1/attempt/start", headers=sh)
    client.post("/api/v1/submissions/",
                json={"question_id": "q1", "answer_text": "4", "quiz_id": "quiz-1"}, headers=sh)
    client.post("/api/v1/quizzes/quiz-1/attempt/finish", headers=sh)

    # students cannot read the roster
    assert client.get("/api/v1/quizzes/quiz-1/attempts", headers=sh).status_code == 403

    rows = client.get("/api/v1/quizzes/quiz-1/attempts",
                      headers=_bearer(token_factory, "instructor")).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["username"] == "alice"
    assert row["status"] == "completed"
    assert row["answered_count"] == 1
    assert row["total_questions"] == 2
    assert row["max_score"] == 5.0
    assert row["duration_seconds"] is not None
