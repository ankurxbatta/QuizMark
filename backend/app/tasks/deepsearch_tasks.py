"""
deepsearch_tasks.py — Celery tasks for DeepSearch RAG retrieval.

Queue: deepsearch_tasks
Worker: worker-deepsearch (concurrency=3)

Separates the multi-query vector retrieval step from question generation
so retrieval bottlenecks never stall the gen or mark workers.

DeepSearch flow:
  1. LLM decomposes chapter topic into 4 exam-focused sub-queries
  2. Each sub-query runs a parallel vector search against pdf_chunks
  3. Results are deduplicated + ranked by teaching density
  4. Top-K chunks are returned to the generation task as rich context

This mirrors Shiksha Copilot's multi-query RAG pattern.
"""
from __future__ import annotations

import asyncio
import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    queue="deepsearch_tasks",
    max_retries=3,
    soft_time_limit=300,
    time_limit=360,
)
def deepsearch_retrieve_task(
    self,
    chapter_topic: str,
    book_id: str,
    top_k: int = 10,
    bloom_level: str | None = None,
) -> list[dict]:
    """
    Multi-query RAG retrieval for a chapter topic.
    Returns a list of serialisable chunk dicts ranked by relevance.
    Used by the gen worker to get rich context before generating questions.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            _run_deepsearch(chapter_topic, book_id, top_k, bloom_level)
        )
    except Exception as exc:
        logger.warning(f"deepsearch_retrieve_task failed: {exc}")
        raise self.retry(exc=exc, countdown=10)
    finally:
        loop.close()


@celery_app.task(
    bind=True,
    queue="deepsearch_tasks",
    max_retries=2,
    soft_time_limit=120,
    time_limit=150,
)
def deepsearch_concept_extract_task(self, chapter_topic: str, book_id: str) -> dict:
    """
    Extract key concepts from a chapter using the LLM.
    Returns { concepts: [...], enriched_topic: str } for use in Round 0 of generation.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_run_concept_extract(chapter_topic, book_id))
    except Exception as exc:
        logger.warning(f"deepsearch_concept_extract_task failed: {exc}")
        raise self.retry(exc=exc, countdown=5)
    finally:
        loop.close()


async def _run_deepsearch(
    chapter_topic: str,
    book_id: str,
    top_k: int,
    bloom_level: str | None,
) -> list[dict]:
    from app.services.question_generator import deep_retrieve_for_generation

    chunks = await deep_retrieve_for_generation(
        topic=chapter_topic,
        book_id=book_id,
        k=top_k,
    )

    # Serialise to plain dicts (Celery result backend stores JSON)
    return [
        {
            "text": getattr(c, "text", ""),
            "chapter_title": getattr(c, "chapter_title", ""),
            "section_title": getattr(c, "section_title", ""),
            "topic_tag": getattr(c, "topic_tag", ""),
            "page_start": getattr(c, "page_start", 0),
            "page_end": getattr(c, "page_end", 0),
            "has_formula": getattr(c, "has_formula", False),
            "has_example": getattr(c, "has_example", False),
            "teaching_density": getattr(c, "teaching_density", 0.0),
            "math_text": getattr(c, "math_text", ""),
            "image_texts": getattr(c, "image_texts", []),
            "table_texts": getattr(c, "table_texts", []),
        }
        for c in chunks
    ]


async def _run_concept_extract(chapter_topic: str, book_id: str) -> dict:
    from app.services.question_generator import extract_chapter_concepts

    try:
        concepts = await extract_chapter_concepts(chapter_topic, book_id)
        enriched = f"{chapter_topic} — {', '.join(concepts[:6])}" if concepts else chapter_topic
        return {"concepts": concepts, "enriched_topic": enriched}
    except Exception as exc:
        logger.warning(f"concept extract failed: {exc}")
        return {"concepts": [], "enriched_topic": chapter_topic}
