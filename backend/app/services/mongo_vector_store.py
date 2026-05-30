"""
mongo_vector_store.py  —  MongoDB Atlas Local vector store for PDF source chunks.

Stores EnhancedChunk documents with 768-dim nomic-embed-text embeddings.
Uses Atlas Vector Search ($vectorSearch aggregation) for semantic retrieval.

All public functions are non-fatal: errors are logged and return empty/zero
results so MongoDB failures never interrupt question generation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Lazy globals — initialised on first call to get_mongo_db()
_mongo_client: Any = None
_mongo_db: Any = None

COLLECTION_NAME = "pdf_chunks"
INDEX_NAME = "pdf_chunks_vector_index"
EMBEDDING_DIMENSIONS = 768


async def _get_collection():
    """Return the pdf_chunks collection, initialising the client if needed."""
    global _mongo_client, _mongo_db

    if _mongo_db is None:
        try:
            import motor.motor_asyncio
            from app.core.config import settings

            if not getattr(settings, "MONGODB_ENABLED", False):
                raise RuntimeError("MONGODB_ENABLED is false")

            _mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
                settings.MONGODB_URL,
                serverSelectionTimeoutMS=5000,
            )
            _mongo_db = _mongo_client[settings.MONGODB_DB_NAME]
            logger.info(f"MongoDB connected: {settings.MONGODB_URL}/{settings.MONGODB_DB_NAME}")
        except Exception as exc:
            logger.error(f"MongoDB connection failed: {exc}")
            raise

    return _mongo_db[COLLECTION_NAME]


async def ensure_vector_index() -> None:
    """
    Create the Atlas Vector Search index on pdf_chunks if it doesn't exist.
    Called once at API startup. Non-fatal if index already exists.
    """
    try:
        collection = await _get_collection()

        # List existing search indexes
        existing_names: set[str] = set()
        try:
            async for idx in await collection.list_search_indexes():
                existing_names.add(idx.get("name", ""))
        except Exception:
            pass  # Atlas Local may not support list_search_indexes in older builds

        if INDEX_NAME in existing_names:
            logger.info(f"MongoDB vector index '{INDEX_NAME}' already exists")
            return

        index_model = {
            "name": INDEX_NAME,
            "type": "vectorSearch",
            "definition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": EMBEDDING_DIMENSIONS,
                        "similarity": "cosine",
                    }
                ]
            },
        }
        await collection.create_search_index(index_model)
        logger.info(f"MongoDB vector index '{INDEX_NAME}' created")

    except Exception as exc:
        logger.warning(f"ensure_vector_index failed (non-fatal): {exc}")


async def store_chunk(chunk: Any, embedding: list[float], book_id: str) -> str:
    """
    Insert one chunk document with its embedding into MongoDB.
    Returns the inserted _id as a string, or "" on failure.
    """
    try:
        collection = await _get_collection()

        doc = {
            "book_id": book_id,
            "chapter_num": chunk.chapter_num,
            "chapter_title": chunk.chapter_title,
            "section_title": chunk.section_title,
            "topic_tag": chunk.topic_tag,
            "text": chunk.text,
            "image_texts": getattr(chunk, "image_texts", []),
            "table_texts": getattr(chunk, "table_texts", []),
            "math_text": getattr(chunk, "math_text", ""),
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "has_images": getattr(chunk, "has_images", False),
            "has_tables": getattr(chunk, "has_tables", False),
            "has_math": getattr(chunk, "has_math_font", chunk.has_formula),
            "has_formula": chunk.has_formula,
            "has_example": chunk.has_example,
            "teaching_density": chunk.teaching_density,
            "key_terms": chunk.key_terms,
            "embedding": embedding,
            "created_at": datetime.now(timezone.utc),
        }

        result = await collection.insert_one(doc)
        return str(result.inserted_id)

    except Exception as exc:
        logger.warning(f"store_chunk failed (non-fatal): {exc}")
        return ""


async def vector_search(
    query_embedding: list[float],
    k: int = 5,
    book_id: str | None = None,
) -> list[dict]:
    """
    Run Atlas Vector Search ($vectorSearch) on the pdf_chunks collection.
    Returns up to k documents (embedding field excluded).
    Returns [] on failure.
    """
    try:
        collection = await _get_collection()

        pipeline: list[dict] = [
            {
                "$vectorSearch": {
                    "index": INDEX_NAME,
                    "path": "embedding",
                    "queryVector": query_embedding,
                    "numCandidates": k * 10,
                    "limit": k,
                }
            },
        ]

        if book_id:
            pipeline.append({"$match": {"book_id": book_id}})

        # Exclude the large embedding array from results
        pipeline.append({"$project": {"embedding": 0}})

        cursor = collection.aggregate(pipeline)
        results = await cursor.to_list(length=k)
        # Convert ObjectId to string for JSON serialisation
        for doc in results:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
        return results

    except Exception as exc:
        logger.warning(f"vector_search failed (non-fatal): {exc}")
        return []


async def delete_book_chunks(book_id: str) -> int:
    """Delete all chunks for a given book_id. Returns count deleted, or 0 on failure."""
    try:
        collection = await _get_collection()
        result = await collection.delete_many({"book_id": book_id})
        count = result.deleted_count
        logger.info(f"Deleted {count} chunks for book_id={book_id}")
        return count
    except Exception as exc:
        logger.warning(f"delete_book_chunks failed (non-fatal): {exc}")
        return 0


async def get_chunk_stats(book_id: str | None = None) -> dict:
    """Return aggregate stats for stored chunks (for debugging/verification)."""
    try:
        collection = await _get_collection()
        match = {"$match": {"book_id": book_id}} if book_id else {"$match": {}}
        pipeline = [
            match,
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": 1},
                    "with_images": {"$sum": {"$cond": ["$has_images", 1, 0]}},
                    "with_tables": {"$sum": {"$cond": ["$has_tables", 1, 0]}},
                    "with_math": {"$sum": {"$cond": ["$has_math", 1, 0]}},
                }
            },
        ]
        cursor = collection.aggregate(pipeline)
        docs = await cursor.to_list(length=1)
        if docs:
            d = docs[0]
            return {
                "total": d.get("total", 0),
                "with_images": d.get("with_images", 0),
                "with_tables": d.get("with_tables", 0),
                "with_math": d.get("with_math", 0),
            }
        return {"total": 0, "with_images": 0, "with_tables": 0, "with_math": 0}
    except Exception as exc:
        logger.warning(f"get_chunk_stats failed: {exc}")
        return {}
