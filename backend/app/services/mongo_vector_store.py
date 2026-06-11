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

CHUNKS_COLLECTION       = "pdf_chunks"
QUESTIONS_COLLECTION    = "questions"
CHECKPOINTS_COLLECTION  = "ingest_checkpoints"
CHUNKS_INDEX_NAME       = "pdf_chunks_vector_index"
QUESTIONS_INDEX_NAME    = "questions_vector_index"
EMBEDDING_DIMENSIONS    = 768


# ── Connection ─────────────────────────────────────────────────────────────────

async def _get_db():
    # Delegate to the shared loop-aware client so Celery tasks (one event loop
    # per execution) never reuse a client bound to a previous task's closed loop.
    from app.core.database import get_mongo_db
    return get_mongo_db()


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

def _chunk_to_doc(chunk: Any, embedding: list[float], book_id: str, book_hash: str | None) -> dict:
    doc = {
        "book_id": book_id,
        "book_hash": book_hash,
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
    if book_hash:
        # Deterministic id: a resumed/crashed ingest re-creating the same chunk
        # hits a duplicate-key no-op instead of inserting a second copy.
        import hashlib
        key = f"{book_hash}:{chunk.page_start}:{chunk.page_end}:{chunk.text[:2000]}"
        doc["_id"] = hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:24]
    return doc


async def store_chunk(chunk: Any, embedding: list[float], book_id: str, book_hash: str | None = None) -> str:
    try:
        col = await _get_collection(CHUNKS_COLLECTION)
        doc = _chunk_to_doc(chunk, embedding, book_id, book_hash)
        if "_id" in doc:
            await col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
            return str(doc["_id"])
        result = await col.insert_one(doc)
        return str(result.inserted_id)
    except Exception as exc:
        logger.warning(f"store_chunk failed (non-fatal): {exc}")
        return ""


async def store_chunks_bulk(
    chunks: list,
    embeddings: list[list[float]],
    book_id: str,
    book_hash: str | None = None,
) -> int:
    """
    Bulk-insert a window of chunks. Skips chunks with an empty embedding (so a
    single embed failure doesn't poison the whole window). Returns inserted count.
    """
    if not chunks:
        return 0
    docs = [
        _chunk_to_doc(chunks[i], embeddings[i], book_id, book_hash)
        for i in range(min(len(chunks), len(embeddings)))
        if embeddings[i]
    ]
    if not docs:
        return 0
    try:
        col = await _get_collection(CHUNKS_COLLECTION)
        result = await col.insert_many(docs, ordered=False)
        return len(result.inserted_ids)
    except Exception as exc:
        # Duplicate _ids (chunk re-created after a crash/resume) are expected —
        # ordered=False means all non-duplicates were still inserted.
        details = getattr(exc, "details", None) or {}
        n_inserted = int(details.get("nInserted", 0))
        write_errors = details.get("writeErrors", [])
        if write_errors and all(e.get("code") == 11000 for e in write_errors):
            return n_inserted
        logger.warning(f"store_chunks_bulk failed (non-fatal): {exc}")
        return n_inserted


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


async def delete_book_chunks(book_id: str | None = None, book_hash: str | None = None) -> int:
    """Delete chunks by book_id, book_hash, or both. Returns deleted_count."""
    if not book_id and not book_hash:
        return 0
    filt: dict = {}
    if book_id:
        filt["book_id"] = book_id
    if book_hash:
        filt["book_hash"] = book_hash
    try:
        col = await _get_collection(CHUNKS_COLLECTION)
        result = await col.delete_many(filt)
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


# ── Book PDF storage (GridFS) ──────────────────────────────────────────────────
# The uploaded PDF is stored once per book_hash so Celery messages carry only
# ids — re-queues of big books no longer push ~30MB of base64 through Redis.

PDF_BUCKET = "book_pdfs"


async def save_book_pdf(book_hash: str, filename: str, data: bytes) -> bool:
    """Store the PDF in GridFS keyed by book_hash (no-op if already stored)."""
    try:
        from motor.motor_asyncio import AsyncIOMotorGridFSBucket
        db = await _get_db()
        existing = await db[f"{PDF_BUCKET}.files"].find_one(
            {"metadata.book_hash": book_hash}, {"_id": 1}
        )
        if existing:
            return True
        bucket = AsyncIOMotorGridFSBucket(db, bucket_name=PDF_BUCKET)
        await bucket.upload_from_stream(
            filename, data, metadata={"book_hash": book_hash}
        )
        return True
    except Exception as exc:
        logger.warning(f"save_book_pdf failed: {exc}")
        return False


async def load_book_pdf(book_hash: str) -> bytes | None:
    try:
        from motor.motor_asyncio import AsyncIOMotorGridFSBucket
        db = await _get_db()
        f = await db[f"{PDF_BUCKET}.files"].find_one(
            {"metadata.book_hash": book_hash}, {"_id": 1}
        )
        if not f:
            return None
        bucket = AsyncIOMotorGridFSBucket(db, bucket_name=PDF_BUCKET)
        stream = await bucket.open_download_stream(f["_id"])
        return await stream.read()
    except Exception as exc:
        logger.warning(f"load_book_pdf failed: {exc}")
        return None


async def delete_book_pdf(book_hash: str) -> None:
    try:
        from motor.motor_asyncio import AsyncIOMotorGridFSBucket
        db = await _get_db()
        bucket = AsyncIOMotorGridFSBucket(db, bucket_name=PDF_BUCKET)
        cursor = db[f"{PDF_BUCKET}.files"].find(
            {"metadata.book_hash": book_hash}, {"_id": 1}
        )
        async for f in cursor:
            await bucket.delete(f["_id"])
    except Exception as exc:
        logger.warning(f"delete_book_pdf failed: {exc}")


# ── Ingest checkpoints (resumable page-by-page ingestion) ──────────────────────

async def get_checkpoint(book_hash: str) -> dict | None:
    """Return the checkpoint document for a book_hash, or None."""
    try:
        col = await _get_collection(CHECKPOINTS_COLLECTION)
        return await col.find_one({"_id": book_hash})
    except Exception as exc:
        logger.warning(f"get_checkpoint failed: {exc}")
        return None


async def save_checkpoint(book_hash: str, fields: dict) -> None:
    """Upsert a checkpoint. `fields` is merged via $set; updated_at is stamped here."""
    try:
        col = await _get_collection(CHECKPOINTS_COLLECTION)
        payload = dict(fields)
        payload["updated_at"] = datetime.now(timezone.utc)
        payload.setdefault("created_at", payload["updated_at"])
        await col.update_one(
            {"_id": book_hash},
            {"$set": payload, "$setOnInsert": {"_id": book_hash}},
            upsert=True,
        )
    except Exception as exc:
        logger.warning(f"save_checkpoint failed: {exc}")


async def delete_checkpoint(book_hash: str) -> int:
    try:
        col = await _get_collection(CHECKPOINTS_COLLECTION)
        result = await col.delete_one({"_id": book_hash})
        return result.deleted_count
    except Exception as exc:
        logger.warning(f"delete_checkpoint failed: {exc}")
        return 0


async def list_incomplete_checkpoints(limit: int = 100) -> list[dict]:
    """List in-progress (non-complete) checkpoints, newest-updated first."""
    try:
        col = await _get_collection(CHECKPOINTS_COLLECTION)
        cursor = col.find(
            {"status": {"$ne": "complete"}},
            projection={"state": 0},  # state is heavy; omit from list view
        ).sort("updated_at", -1).limit(limit)
        return await cursor.to_list(length=limit)
    except Exception as exc:
        logger.warning(f"list_incomplete_checkpoints failed: {exc}")
        return []


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
