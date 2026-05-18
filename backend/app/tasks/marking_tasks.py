import asyncio
from app.tasks.celery_app import celery_app
from app.core.database import AsyncSessionLocal
from app.services.rag_pipeline import mark_submission


@celery_app.task(bind=True, max_retries=3)
def mark_submission_task(self, submission_id: str):
    """Async marking job dispatched to the Celery worker."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def _run():
            async with AsyncSessionLocal() as db:
                return await mark_submission(submission_id, db)
        return loop.run_until_complete(_run())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)
    finally:
        loop.close()
