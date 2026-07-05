"""
API tests for POST /api/v1/submissions/ (submit_answer).

Covers RBAC, question existence, assignment (direct + via-quiz), the happy
path (task dispatch), the plain duplicate 409 (fast-path pre-check) and the
TOCTOU-race 409 (insert_one hits the UNIQUE (student_id, question_id) index and
raises DuplicateKeyError). LLM/task work is mocked: mark_submission_task.delay
is monkeypatched so no real Celery task runs.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from pymongo.errors import DuplicateKeyError

from app.api.v1 import submissions as submissions_mod


def _bearer(token_factory, role="student", sub="user-1"):
    return {"Authorization": f"Bearer {token_factory(role, sub)}"}


async def _seed_student(db, sid="user-1", username="stud"):
    await db["users"].insert_one({"_id": sid, "username": username, "role": "student"})


async def _seed_question(db, qid="q1", assigned=None):
    await db["questions"].insert_one({
        "_id": qid,
        "question_text": "What is 2+2?",
        "question_type": "short_answer",
        "model_answer": "4",
        "rubric": "",
        "max_marks": 2,
        "assigned_student_ids": assigned or [],
        "created_at": datetime.now(timezone.utc),
    })


@pytest.fixture()
def fake_delay(monkeypatch):
    """Replace mark_submission_task.delay with a MagicMock (no real task)."""
    m = MagicMock()
    monkeypatch.setattr(submissions_mod.mark_submission_task, "delay", m)
    return m


# ── RBAC / existence ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_forbidden_for_non_student(client, token_factory, mock_db, fake_delay):
    # The endpoint reads the *DB* user role, not the token role.
    await mock_db["users"].insert_one({"_id": "user-1", "username": "prof", "role": "instructor"})
    r = client.post(
        "/api/v1/submissions/",
        json={"question_id": "q1", "answer_text": "x"},
        headers=_bearer(token_factory, "instructor"),
    )
    assert r.status_code == 403
    fake_delay.assert_not_called()


@pytest.mark.asyncio
async def test_submit_unknown_question_404(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db)
    r = client.post(
        "/api/v1/submissions/",
        json={"question_id": "does-not-exist", "answer_text": "x"},
        headers=_bearer(token_factory),
    )
    assert r.status_code == 404
    fake_delay.assert_not_called()


@pytest.mark.asyncio
async def test_submit_not_assigned_403(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db)
    await _seed_question(mock_db, "q1", assigned=[])  # not assigned, no quiz
    r = client.post(
        "/api/v1/submissions/",
        json={"question_id": "q1", "answer_text": "x"},
        headers=_bearer(token_factory),
    )
    assert r.status_code == 403
    fake_delay.assert_not_called()


# ── Happy path ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_happy_path_directly_assigned(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db)
    await _seed_question(mock_db, "q1", assigned=["user-1"])
    r = client.post(
        "/api/v1/submissions/",
        json={"question_id": "q1", "answer_text": "four"},
        headers=_bearer(token_factory),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["student_id"] == "user-1"
    assert body["question_id"] == "q1"
    # Response is joined with the question metadata.
    assert body["question_text"] == "What is 2+2?"
    assert body["max_marks"] == 2
    # Marking task dispatched exactly once with the new submission id.
    fake_delay.assert_called_once_with(body["id"])
    # Persisted.
    assert await mock_db["submissions"].find_one({"_id": body["id"]}) is not None


@pytest.mark.asyncio
async def test_submit_grant_via_quiz(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db)
    # Question is NOT directly assigned...
    await _seed_question(mock_db, "q1", assigned=[])
    # ...but is reachable through a quiz assigned to the student.
    await mock_db["quizzes"].insert_one({
        "_id": "quiz-1",
        "title": "Algebra",
        "question_ids": ["q1"],
        "assigned_student_ids": ["user-1"],
    })
    r = client.post(
        "/api/v1/submissions/",
        json={"question_id": "q1", "answer_text": "four"},
        headers=_bearer(token_factory),
    )
    assert r.status_code == 201, r.text
    fake_delay.assert_called_once()


# ── Duplicate / TOCTOU race ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_duplicate_precheck_409(client, token_factory, mock_db, fake_delay):
    await _seed_student(mock_db)
    await _seed_question(mock_db, "q1", assigned=["user-1"])
    first = client.post(
        "/api/v1/submissions/",
        json={"question_id": "q1", "answer_text": "four"},
        headers=_bearer(token_factory),
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/v1/submissions/",
        json={"question_id": "q1", "answer_text": "four again"},
        headers=_bearer(token_factory),
    )
    assert second.status_code == 409
    # Only the first submission dispatched a marking task.
    fake_delay.assert_called_once()
    # Exactly one submission exists.
    assert await mock_db["submissions"].count_documents({"student_id": "user-1"}) == 1


@pytest.mark.asyncio
async def test_submit_duplicate_race_returns_409(client, token_factory, mock_db, fake_delay):
    """
    Simulate the concurrent double-submit: the fast-path pre-check misses (the
    competing insert hasn't landed yet) but insert_one loses the race against
    the UNIQUE index and raises DuplicateKeyError. The endpoint must still
    return the clean 409 (not a 500).
    """
    await _seed_student(mock_db)
    await _seed_question(mock_db, "q1", assigned=["user-1"])

    # Wrap the db so the submissions pre-check returns None (race window) and
    # insert_one raises DuplicateKeyError, while every other collection behaves
    # normally.
    class _RaceSubmissions:
        def __init__(self, real):
            self._real = real

        async def find_one(self, *a, **k):
            return None  # pre-check misses under the race

        async def insert_one(self, *a, **k):
            raise DuplicateKeyError("E11000 duplicate key")

    class _WrapDB:
        def __init__(self, real):
            self._real = real

        def __getitem__(self, name):
            if name == "submissions":
                return _RaceSubmissions(self._real[name])
            return self._real[name]

    from app.core.database import get_db
    from app.main import app
    app.dependency_overrides[get_db] = lambda: _WrapDB(mock_db)

    r = client.post(
        "/api/v1/submissions/",
        json={"question_id": "q1", "answer_text": "four"},
        headers=_bearer(token_factory),
    )
    assert r.status_code == 409
    fake_delay.assert_not_called()
