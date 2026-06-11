"""
index_tasks.py — specialist RAG index builders (MULTI_RAG_DESIGN).

Each builder runs on its specialist worker's queue:
  math   → math_tasks   (worker-math)
  figure → vision_tasks (worker-vision)
  table  → clean_tasks  (worker-clean)
Builders read already-stored chunks — no PDF access, no Redis payloads.
"""
import asyncio
import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_build(task_self, builder, book_id: str, name: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        stats = loop.run_until_complete(builder(book_id))
        logger.info(f"{name} done: {stats}")
        return stats
    except Exception as exc:
        logger.warning(
            "%s attempt %d/%d failed for '%s': %s",
            name, task_self.request.retries + 1, task_self.max_retries + 1, book_id, exc,
        )
        if task_self.request.retries < task_self.max_retries:
            raise task_self.retry(exc=exc, countdown=30)
        raise
    finally:
        loop.close()


@celery_app.task(bind=True, queue="math_tasks", max_retries=2, soft_time_limit=1500, time_limit=1800)
def build_math_index_task(self, book_id: str):
    """Build (or rebuild) the math formula index for one book."""
    from app.services.math_index import build_math_index
    return _run_build(self, build_math_index, book_id, "build_math_index_task")


@celery_app.task(bind=True, queue="vision_tasks", max_retries=2, soft_time_limit=1500, time_limit=1800)
def build_figure_index_task(self, book_id: str):
    """Build (or rebuild) the figure/chart index for one book."""
    from app.services.figure_index import build_figure_index
    return _run_build(self, build_figure_index, book_id, "build_figure_index_task")


@celery_app.task(bind=True, queue="clean_tasks", max_retries=2, soft_time_limit=1500, time_limit=1800)
def build_table_index_task(self, book_id: str):
    """Build (or rebuild) the table index for one book."""
    from app.services.table_index import build_table_index
    return _run_build(self, build_table_index, book_id, "build_table_index_task")


@celery_app.task(bind=True, queue="embed_tasks", max_retries=2, soft_time_limit=1500, time_limit=1800)
def rebuild_index_embeddings_task(self, index_name: str, book_id: str):
    """
    Re-embed one specialist index for a book (worker-embed). Enrichment is
    cached by content hash, so a rebuild only re-pays the embedding calls —
    used after changing embedding providers or enrichment prompts.
    """
    builders = {}
    from app.services.math_index import build_math_index
    from app.services.figure_index import build_figure_index
    from app.services.table_index import build_table_index
    builders = {"math": build_math_index, "figure": build_figure_index, "table": build_table_index}
    builder = builders.get(index_name)
    if builder is None:
        raise ValueError(f"Unknown index '{index_name}' (expected one of {sorted(builders)})")
    return _run_build(self, builder, book_id, f"rebuild_index_embeddings_task[{index_name}]")
