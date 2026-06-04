"""
clean_tasks.py — Celery tasks for cleaning PDF noise from stored chunks.

Queue: clean_tasks
Worker: worker-clean (concurrency=4, CPU-only, no API calls)

Tasks:
  clean_book_chunks_task(book_id)   — clean all chunks for a book
  clean_all_chunks_task()           — clean every chunk in the database
  clean_chunk_by_id_task(chunk_id)  — clean a single chunk (for re-processing)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.tasks.celery_app import celery_app
from app.core.database import get_mongo_db
from app.services.text_cleaner import clean_chunk_doc, estimate_noise_ratio

logger = logging.getLogger(__name__)

_BATCH = 200  # chunks processed per DB round-trip


@celery_app.task(
    bind=True,
    queue="clean_tasks",
    max_retries=2,
    soft_time_limit=1800,
    time_limit=2100,
)
def clean_book_chunks_task(self, book_id: str) -> dict:
    """Clean all stored chunks for a specific book."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_clean_book(book_id))
    except Exception as exc:
        logger.error(f"clean_book_chunks_task failed for {book_id}: {exc}")
        raise self.retry(exc=exc, countdown=30)
    finally:
        loop.close()


@celery_app.task(
    bind=True,
    queue="clean_tasks",
    max_retries=1,
    soft_time_limit=3600,
    time_limit=3900,
)
def clean_all_chunks_task(self) -> dict:
    """Clean every pdf_chunks document in the database. Use after bulk ingestion."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_clean_all())
    except Exception as exc:
        logger.error(f"clean_all_chunks_task failed: {exc}")
        raise
    finally:
        loop.close()


@celery_app.task(
    bind=True,
    queue="clean_tasks",
    max_retries=3,
    soft_time_limit=60,
    time_limit=90,
)
def clean_chunk_by_id_task(self, chunk_id: str) -> dict:
    """Clean a single chunk by its _id. Used for spot re-processing."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_clean_one(chunk_id))
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5)
    finally:
        loop.close()


# ── Async implementations ──────────────────────────────────────────────────────

async def _clean_book(book_id: str) -> dict:
    db = get_mongo_db()
    query = {"book_id": book_id}
    total = await db["pdf_chunks"].count_documents(query)
    if total == 0:
        return {"book_id": book_id, "cleaned": 0, "skipped": 0, "total": 0}

    cleaned = 0
    skipped = 0
    skip = 0

    while True:
        batch = await db["pdf_chunks"].find(query).skip(skip).limit(_BATCH).to_list(length=_BATCH)
        if not batch:
            break

        bulk_ops = []
        for doc in batch:
            noise = estimate_noise_ratio(doc.get("text", ""))
            # Always clean — noise_ratio 0 still benefits from normalisation
            original_text = doc.get("text", "")
            cleaned_doc = clean_chunk_doc(dict(doc))
            if cleaned_doc["text"] != original_text or noise > 0.01:
                from pymongo import UpdateOne
                bulk_ops.append(UpdateOne(
                    {"_id": doc["_id"]},
                    {"$set": {
                        "text": cleaned_doc["text"],
                        "math_text": cleaned_doc.get("math_text", ""),
                        "image_texts": cleaned_doc.get("image_texts", []),
                        "table_texts": cleaned_doc.get("table_texts", []),
                        "key_terms": cleaned_doc.get("key_terms", []),
                        "cleaned_at": datetime.now(timezone.utc),
                        "noise_ratio": round(noise, 4),
                    }},
                ))
                cleaned += 1
            else:
                skipped += 1

        if bulk_ops:
            await db["pdf_chunks"].bulk_write(bulk_ops, ordered=False)

        skip += len(batch)
        logger.info(f"[clean] {book_id}: {skip}/{total} processed, {cleaned} updated")

        if len(batch) < _BATCH:
            break

    logger.info(f"[clean] Done — book={book_id} cleaned={cleaned} skipped={skipped} total={total}")
    return {"book_id": book_id, "cleaned": cleaned, "skipped": skipped, "total": total}


async def _clean_all() -> dict:
    db = get_mongo_db()
    # Get distinct book_ids and clean each
    book_ids = await db["pdf_chunks"].distinct("book_id")
    if not book_ids:
        return {"cleaned": 0, "total": 0, "books": []}

    total_cleaned = 0
    total_docs = 0
    results = []
    for book_id in book_ids:
        r = await _clean_book(book_id or "unknown")
        total_cleaned += r["cleaned"]
        total_docs += r["total"]
        results.append(r)

    return {"cleaned": total_cleaned, "total": total_docs, "books": results}


async def _clean_one(chunk_id: str) -> dict:
    db = get_mongo_db()
    doc = await db["pdf_chunks"].find_one({"_id": chunk_id})
    if not doc:
        return {"chunk_id": chunk_id, "found": False}

    noise = estimate_noise_ratio(doc.get("text", ""))
    cleaned_doc = clean_chunk_doc(dict(doc))
    await db["pdf_chunks"].update_one(
        {"_id": chunk_id},
        {"$set": {
            "text": cleaned_doc["text"],
            "math_text": cleaned_doc.get("math_text", ""),
            "image_texts": cleaned_doc.get("image_texts", []),
            "table_texts": cleaned_doc.get("table_texts", []),
            "key_terms": cleaned_doc.get("key_terms", []),
            "cleaned_at": datetime.now(timezone.utc),
            "noise_ratio": round(noise, 4),
        }},
    )
    return {"chunk_id": chunk_id, "found": True, "noise_ratio": round(noise, 4)}
