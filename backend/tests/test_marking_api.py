"""
API tests for PUT /api/v1/marking/{id}/override (override_mark).

Covers the instructor override flow (mark update, flag clear, audit log with the
correct old_mark) and RBAC, plus the fix that makes the override response join
the question so question_text / max_marks are non-null and consistent with the
submissions producer of SubmissionOut.
"""
from datetime import datetime, timezone

import pytest


def _ibearer(token_factory, sub="prof-1"):
    return {"Authorization": f"Bearer {token_factory('instructor', sub)}"}


async def _seed_question(db, qid="q1"):
    await db["questions"].insert_one({
        "_id": qid,
        "question_text": "Explain photosynthesis.",
        "question_type": "short_answer",
        "model_answer": "...",
        "rubric": "",
        "max_marks": 5,
        "assigned_student_ids": [],
        "created_at": datetime.now(timezone.utc),
    })


async def _seed_submission(db, sid="sub-1", qid="q1", auto_mark=1.5):
    await db["submissions"].insert_one({
        "_id": sid,
        "student_id": "stud-1",
        "question_id": qid,
        "answer_text": "A plant thing.",
        "auto_mark": auto_mark,
        "auto_feedback": "weak",
        "auto_confidence": 0.9,
        "marking_route": "MID",
        "override_mark": None,
        "override_feedback": None,
        "override_reason": None,
        "is_flagged": True,
        "is_marked": True,
        "submitted_at": datetime.now(timezone.utc),
        "marked_at": datetime.now(timezone.utc),
    })


@pytest.mark.asyncio
async def test_override_updates_mark_clears_flag_and_audits(client, token_factory, mock_db):
    await _seed_question(mock_db, "q1")
    await _seed_submission(mock_db, "sub-1", "q1", auto_mark=1.5)

    r = client.put(
        "/api/v1/marking/sub-1/override",
        json={"override_mark": 4.0, "override_feedback": "Much better", "override_reason": "Re-read"},
        headers=_ibearer(token_factory),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Mark updated + flag cleared.
    assert body["override_mark"] == 4.0
    assert body["is_flagged"] is False

    # Response now carries the joined question metadata (the fix).
    assert body["question_text"] == "Explain photosynthesis."
    assert body["max_marks"] == 5

    # Persisted state matches.
    updated = await mock_db["submissions"].find_one({"_id": "sub-1"})
    assert updated["override_mark"] == 4.0
    assert updated["is_flagged"] is False

    # Audit log written with the correct old_mark (the prior auto_mark).
    log = await mock_db["audit_logs"].find_one({"submission_id": "sub-1"})
    assert log is not None
    assert log["event_type"] == "override"
    assert log["old_mark"] == 1.5
    assert log["actor_id"] == "prof-1"


@pytest.mark.asyncio
async def test_override_old_mark_prefers_prior_override(client, token_factory, mock_db):
    await _seed_question(mock_db, "q1")
    await _seed_submission(mock_db, "sub-2", "q1", auto_mark=1.5)
    # A prior override existed → old_mark should be that, not the auto_mark.
    await mock_db["submissions"].update_one({"_id": "sub-2"}, {"$set": {"override_mark": 3.0}})

    r = client.put(
        "/api/v1/marking/sub-2/override",
        json={"override_mark": 5.0, "override_feedback": "Perfect"},
        headers=_ibearer(token_factory),
    )
    assert r.status_code == 200, r.text
    log = await mock_db["audit_logs"].find_one({"submission_id": "sub-2"})
    assert log["old_mark"] == 3.0


@pytest.mark.asyncio
async def test_override_forbidden_for_student(client, token_factory, mock_db):
    await _seed_question(mock_db, "q1")
    await _seed_submission(mock_db, "sub-3", "q1")
    r = client.put(
        "/api/v1/marking/sub-3/override",
        json={"override_mark": 4.0, "override_feedback": "x"},
        headers={"Authorization": f"Bearer {token_factory('student', 'stud-1')}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_override_unknown_submission_404(client, token_factory, mock_db):
    r = client.put(
        "/api/v1/marking/missing/override",
        json={"override_mark": 4.0, "override_feedback": "x"},
        headers=_ibearer(token_factory),
    )
    assert r.status_code == 404
