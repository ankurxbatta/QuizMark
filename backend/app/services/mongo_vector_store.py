"""
mongo_vector_store.py  —  MongoDB data layer (primary store + vector search).

Collections:
  pdf_chunks            — textbook chunks with 768-dim embeddings (RAG source)
  questions             — generated questions with 768-dim embeddings
  page_description_cache — GPT-4o Vision chart description cache

Vector search indexes (Atlas Vector Search):
  pdf_chunks_vector_index  — cosine similarity on pdf_chunks.embedding
  questions_vector_index   — cosine similarity on questions.embedding

All public functions are non-fatal for vector operations: errors are logged
and return empty/zero results so failures never interrupt the main pipeline.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_mongo_client: Any = None
_mongo_db: Any = None

CHUNKS_COLLECTION     = "pdf_chunks"
QUESTIONS_COLLECTION  = "questions"
CHUNKS_INDEX_NAME     = "pdf_chunks_vector_index"
QUESTIONS_INDEX_NAME  = "questions_vector_index"
EMBEDDING_DIMENSIONS  = 768


# ── Connection ─────────────────────────────────────────────────────────────────

async def _get_db():
    global _mongo_client, _mongo_db
    if _mongo_db is None:
        import motor.motor_asyncio
        from app.core.config import settings
        _mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
            settings.MONGODB_URL,
            serverSelectionTimeoutMS=5000,
        )
        _mongo_db = _mongo_client[settings.MONGODB_DB_NAME]
        logger.info(f"MongoDB connected: {settings.MONGODB_URL}/{settings.MONGODB_DB_NAME}")
    return _mongo_db


async def _get_collection(name: str):
    db = await _get_db()
    return db[name]


# ── Index setup ────────────────────────────────────────────────────────────────

async def _ensure_index(collection_name: str, index_name: str) -> None:
    try:
        col = await _get_collection(collection_name)
        existing: set[str] = set()
        try:
            async for idx in await col.list_search_indexes():
                existing.add(idx.get("name", ""))
        except Exception:
            pass
        if index_name in existing:
            return
        index_model = {
            "name": index_name,
            "type": "vectorSearch",
            "definition": {
                "fields": [{
                    "type": "vector",
                    "path": "embedding",
                    "numDimensions": EMBEDDING_DIMENSIONS,
                    "similarity": "cosine",
                }]
            },
        }
        await col.create_search_index(index_model)
        logger.info(f"MongoDB vector index '{index_name}' created on '{collection_name}'")
    except Exception as exc:
        logger.warning(f"ensure_index({collection_name}) failed (non-fatal): {exc}")


async def ensure_vector_index() -> None:
    """Create vector search indexes for both pdf_chunks and questions."""
    await _ensure_index(CHUNKS_COLLECTION, CHUNKS_INDEX_NAME)
    await _ensure_index(QUESTIONS_COLLECTION, QUESTIONS_INDEX_NAME)


# ── PDF chunk store ────────────────────────────────────────────────────────────

async def store_chunk(chunk: Any, embedding: list[float], book_id: str) -> str:
    try:
        col = await _get_collection(CHUNKS_COLLECTION)
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
        result = await col.insert_one(doc)
        return str(result.inserted_id)
    except Exception as exc:
        logger.warning(f"store_chunk failed (non-fatal): {exc}")
        return ""


async def vector_search(
    query_embedding: list[float],
    k: int = 5,
    book_id: str | None = None,
) -> list[dict]:
    """Semantic search over pdf_chunks. Returns up to k docs (no embedding field)."""
    try:
        col = await _get_collection(CHUNKS_COLLECTION)
        pipeline: list[dict] = [{
            "$vectorSearch": {
                "index": CHUNKS_INDEX_NAME,
                "path": "embedding",
                "queryVector": query_embedding,
                "numCandidates": k * 10,
                "limit": k,
            }
        }]
        if book_id:
            pipeline.append({"$match": {"book_id": book_id}})
        pipeline.append({"$project": {"embedding": 0}})
        results = await col.aggregate(pipeline).to_list(length=k)
        for doc in results:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
        return results
    except Exception as exc:
        logger.warning(f"vector_search (chunks) failed (non-fatal): {exc}")
        return []


async def delete_book_chunks(book_id: str) -> int:
    try:
        col = await _get_collection(CHUNKS_COLLECTION)
        result = await col.delete_many({"book_id": book_id})
        return result.deleted_count
    except Exception as exc:
        logger.warning(f"delete_book_chunks failed (non-fatal): {exc}")
        return 0


async def get_chunk_stats(book_id: str | None = None) -> dict:
    try:
        col = await _get_collection(CHUNKS_COLLECTION)
        match = {"$match": {"book_id": book_id}} if book_id else {"$match": {}}
        pipeline = [
            match,
            {"$group": {
                "_id": None,
                "total": {"$sum": 1},
                "with_images": {"$sum": {"$cond": ["$has_images", 1, 0]}},
                "with_tables": {"$sum": {"$cond": ["$has_tables", 1, 0]}},
                "with_math": {"$sum": {"$cond": ["$has_math", 1, 0]}},
            }},
        ]
        docs = await col.aggregate(pipeline).to_list(length=1)
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


# ── Question vector search ─────────────────────────────────────────────────────

async def search_similar_questions(
    query_embedding: list[float],
    k: int = 3,
) -> list[dict]:
    """Semantic search over stored questions. Returns up to k docs."""
    try:
        col = await _get_collection(QUESTIONS_COLLECTION)
        pipeline = [
            {
                "$vectorSearch": {
                    "index": QUESTIONS_INDEX_NAME,
                    "path": "embedding",
                    "queryVector": query_embedding,
                    "numCandidates": k * 10,
                    "limit": k,
                }
            },
            {"$project": {"embedding": 0}},
        ]
        results = await col.aggregate(pipeline).to_list(length=k)
        for doc in results:
            doc["id"] = str(doc.pop("_id", ""))
        return results
    except Exception as exc:
        logger.warning(f"search_similar_questions failed (non-fatal): {exc}")
        return []
