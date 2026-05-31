import asyncio
import logging
from datetime import datetime, timezone

from app.tasks.celery_app import celery_app
from app.core.database import get_mongo_db
from app.services.rag_pipeline import mark_submission

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3)
def mark_submission_task(self, submission_id: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
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
        loop.close()


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
