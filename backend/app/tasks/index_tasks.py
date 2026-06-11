"""
index_tasks.py — specialist RAG index builders (MULTI_RAG_DESIGN).

Phase 1: math index, built on the math_tasks queue (worker-math).
Builders read already-stored chunks — no PDF access, no Redis payloads.
"""
import asyncio
import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, queue="math_tasks", max_retries=2, soft_time_limit=1500, time_limit=1800)
def build_math_index_task(self, book_id: str):
    """Build (or rebuild) the math formula index for one book."""
    from app.services.math_index import build_math_index

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        stats = loop.run_until_complete(build_math_index(book_id))
        logger.info(f"build_math_index_task done: {stats}")
        return stats
    except Exception as exc:
        logger.warning(
            "build_math_index_task attempt %d/%d failed for '%s': %s",
            self.request.retries + 1, self.max_retries + 1, book_id, exc,
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30)
        raise
    finally:
        loop.close()
