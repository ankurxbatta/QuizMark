from datetime import datetime, timedelta, timezone

from app.tasks import marking_tasks


async def test_claim_is_exclusive(mock_db, monkeypatch):
    monkeypatch.setattr(marking_tasks, "get_mongo_db", lambda: mock_db)
    await mock_db["submissions"].insert_one({"_id": "s1", "student_answer": "x"})

    assert await marking_tasks._claim_submission("s1") is True
    assert await marking_tasks._claim_submission("s1") is False  # second worker loses


async def test_stale_claim_can_be_retaken(mock_db, monkeypatch):
    monkeypatch.setattr(marking_tasks, "get_mongo_db", lambda: mock_db)
    stale = datetime.now(timezone.utc) - timedelta(minutes=marking_tasks.STALE_CLAIM_MINUTES + 5)
    await mock_db["submissions"].insert_one(
        {"_id": "s2", "marking_in_progress": True, "marking_started_at": stale}
    )

    assert await marking_tasks._claim_submission("s2") is True


async def test_missing_submission_cannot_be_claimed(mock_db, monkeypatch):
    monkeypatch.setattr(marking_tasks, "get_mongo_db", lambda: mock_db)
    assert await marking_tasks._claim_submission("does-not-exist") is False
