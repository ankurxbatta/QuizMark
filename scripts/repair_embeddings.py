#!/usr/bin/env python3
"""
repair_embeddings.py — Re-generate missing Gemini embeddings for chunks in MongoDB.

Run from project root:
    python3 scripts/repair_embeddings.py

This script:
1. Finds all pdf_chunks missing embeddings
2. Re-generates via Gemini batchEmbedContents (up to 100 per request)
3. Updates MongoDB in-place
4. Reports completion stats
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("repair_embeddings")

_REPO_ROOT = Path(__file__).parent.parent
_ENV_FILE = _REPO_ROOT / ".env"


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return env
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


_ENV = _load_env()

MONGODB_URL = os.environ.get("MONGODB_URL", _ENV.get("MONGODB_URL", "mongodb://localhost:27017"))
MONGODB_DB = os.environ.get("MONGODB_DB_NAME", _ENV.get("MONGODB_DB_NAME", "marking_tools"))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", _ENV.get("GEMINI_API_KEY", ""))
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_EMBED_MODEL = "gemini-embedding-001"

BOOK_ID = "IntroductoryBusinessStatistics-OP"
BATCH_SIZE = 20   # chunks per Gemini batch request (conservative for rate limits)
INTER_BATCH_DELAY = 4.0  # seconds between batch requests


try:
    import httpx
except ImportError:
    sys.exit("httpx not installed.")

try:
    from pymongo import MongoClient
    from bson import ObjectId
except ImportError:
    sys.exit("pymongo not installed.")


def _build_embed_text(doc: dict) -> str:
    parts = [
        f"{doc.get('chapter_title', '')} {doc.get('section_title', '')}",
        doc.get("text", "")[:1500],
    ]
    if doc.get("table_texts"):
        parts.append("Tables:\n" + "\n".join(doc["table_texts"])[:1200])
    if doc.get("image_texts"):
        parts.append("Images and charts:\n" + "\n".join(doc["image_texts"])[:1200])
    if doc.get("math_text"):
        parts.append(f"Math formulas:\n{doc['math_text'][:800]}")
    return "\n\n".join(p for p in parts if p.strip())


async def _batch_embed(texts: list[str], client: httpx.AsyncClient) -> list[list[float]]:
    """Batch embed up to BATCH_SIZE texts via Gemini batchEmbedContents."""
    requests = [
        {
            "model": f"models/{GEMINI_EMBED_MODEL}",
            "content": {"parts": [{"text": t[:2048]}]},
            "taskType": "SEMANTIC_SIMILARITY",
            "outputDimensionality": 768,
        }
        for t in texts
    ]
    payload = {"requests": requests}

    for attempt in range(5):
        try:
            resp = await client.post(
                f"{GEMINI_BASE}/models/{GEMINI_EMBED_MODEL}:batchEmbedContents",
                params={"key": GEMINI_API_KEY},
                json=payload,
                timeout=60,
            )
            if resp.status_code == 429:
                wait = 15 * (2 ** attempt)
                log.warning(f"Rate limited (429), waiting {wait}s before retry {attempt+1}/5…")
                await asyncio.sleep(wait)
                continue
            if resp.status_code in {500, 503}:
                wait = 5 * (attempt + 1)
                log.warning(f"Server error {resp.status_code}, waiting {wait}s…")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            return [e["values"] for e in data.get("embeddings", [])]
        except httpx.TimeoutException:
            log.warning(f"Timeout on attempt {attempt+1}, retrying…")
            await asyncio.sleep(5)

    log.error("Failed to get embeddings after 5 attempts, returning empty")
    return [[] for _ in texts]


async def main():
    if not GEMINI_API_KEY:
        sys.exit("GEMINI_API_KEY not set in .env")

    mongo_url = MONGODB_URL.replace("mongodb://mongodb:", "mongodb://localhost:")
    client = MongoClient(mongo_url, serverSelectionTimeoutMS=10000, directConnection=True)
    db = client[MONGODB_DB]
    col = db["pdf_chunks"]

    # Find all chunks missing embeddings
    missing = list(col.find(
        {"book_id": BOOK_ID, "embedding": {"$in": [None, []]}},
        {"_id": 1, "text": 1, "chapter_title": 1, "section_title": 1,
         "table_texts": 1, "image_texts": 1, "math_text": 1, "page_start": 1}
    ).sort("page_start", 1))

    total = len(missing)
    log.info(f"Found {total} chunks missing embeddings for book: {BOOK_ID}")

    if total == 0:
        log.info("All chunks already have embeddings!")
        client.close()
        return

    # Also count total chunks
    all_chunks = col.count_documents({"book_id": BOOK_ID})
    log.info(f"Total chunks: {all_chunks}, will repair: {total} ({100*total//all_chunks}%)")

    updated = 0
    failed = 0
    start_time = time.time()

    async with httpx.AsyncClient() as http:
        for batch_start in range(0, total, BATCH_SIZE):
            batch = missing[batch_start:batch_start + BATCH_SIZE]
            texts = [_build_embed_text(doc) for doc in batch]
            ids = [doc["_id"] for doc in batch]
            pages = [doc.get("page_start", "?") for doc in batch]

            log.info(
                f"Batch {batch_start//BATCH_SIZE + 1}/{(total+BATCH_SIZE-1)//BATCH_SIZE} "
                f"— chunks {batch_start+1}-{batch_start+len(batch)}/{total} "
                f"(pages {pages[0]}-{pages[-1]})"
            )

            embeddings = await _batch_embed(texts, http)

            # Update MongoDB
            for doc_id, emb in zip(ids, embeddings):
                if emb:
                    col.update_one({"_id": doc_id}, {"$set": {"embedding": emb}})
                    updated += 1
                else:
                    failed += 1

            elapsed = time.time() - start_time
            rate = updated / elapsed if elapsed > 0 else 0
            remaining = total - (batch_start + len(batch))
            eta = remaining / rate if rate > 0 else 0
            log.info(f"  Updated: {updated}, Failed: {failed}, ETA: {eta/60:.1f}min")

            if batch_start + BATCH_SIZE < total:
                await asyncio.sleep(INTER_BATCH_DELAY)

    client.close()

    elapsed = time.time() - start_time
    log.info("=" * 60)
    log.info(f"DONE in {elapsed/60:.1f} minutes")
    log.info(f"Successfully updated: {updated}/{total} chunks")
    log.info(f"Failed: {failed}")
    if updated + failed == total:
        log.info(f"Repair complete — {updated} embeddings added to MongoDB")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
