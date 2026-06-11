import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.tasks.celery_app import celery_app
from app.core.database import get_mongo_db
from app.services.rag_pipeline import mark_submission

logger = logging.getLogger(__name__)

# A claim older than this is considered stale (worker crashed mid-marking).
STALE_CLAIM_MINUTES = 10


@celery_app.task(bind=True, max_retries=3)
def mark_submission_task(self, submission_id: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    claimed = False
    try:
        # Atomically claim the submission so duplicate deliveries / concurrent
        # workers don't mark the same submission twice.
        claimed = loop.run_until_complete(_claim_submission(submission_id))
        if not claimed:
            logger.info(
                "Submission %s is already being marked by another worker (or does not exist); skipping.",
                submission_id,
            )
            return None

        async def _run():
            db = get_mongo_db()
            return await mark_submission(submission_id, db)
        return loop.run_until_complete(_run())
    except Exception as exc:
        logger.warning(
            "mark_submission_task attempt %d/%d failed for %s: %s",
            self.request.retries + 1, self.max_retries + 1, submission_id, exc,
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=10)
        # Final failure — write error state so the submission isn't stuck pending
        loop.run_until_complete(_write_marking_error(submission_id, str(exc)))
        raise
    finally:
        if claimed:
            loop.run_until_complete(_clear_marking_flag(submission_id))
        loop.close()


async def _claim_submission(submission_id: str) -> bool:
    """Atomically set marking_in_progress; returns False if another worker holds a fresh claim."""
    db = get_mongo_db()
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=STALE_CLAIM_MINUTES)
    doc = await db["submissions"].find_one_and_update(
        {
            "_id": submission_id,
            "$or": [
                {"marking_in_progress": {"$ne": True}},
                {"marking_started_at": {"$lt": stale_before}},
                {"marking_started_at": None},
            ],
        },
        {"$set": {"marking_in_progress": True, "marking_started_at": now}},
    )
    return doc is not None


async def _clear_marking_flag(submission_id: str) -> None:
    try:
        db = get_mongo_db()
        await db["submissions"].update_one(
            {"_id": submission_id},
            {"$set": {"marking_in_progress": False}},
        )
    except Exception:
        logger.exception("Failed to clear marking_in_progress for submission %s", submission_id)


async def _write_marking_error(submission_id: str, reason: str) -> None:
    try:
        db = get_mongo_db()
        await db["submissions"].update_one(
            {"_id": submission_id},
            {"$set": {
                "is_marked": True,
                "auto_mark": 0.0,
                "auto_feedback": "Automated marking failed after 3 attempts. Please review manually.",
                "is_flagged": True,
                "marking_error": True,
                "marking_error_reason": reason[:500],
                "marked_at": datetime.now(timezone.utc),
            }},
        )
        logger.error("Submission %s permanently failed marking: %s", submission_id, reason)
    except Exception:
        logger.exception("Failed to write error state for submission %s", submission_id)
