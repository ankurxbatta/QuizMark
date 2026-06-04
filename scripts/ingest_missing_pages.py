#!/usr/bin/env python3
"""
ingest_missing_pages.py — Ingest specific pages that were skipped during main ingestion.

These pages were skipped because their text was below MIN_CHUNK_CHARS (300 chars).
This script extracts them with a much lower threshold and inserts as supplemental chunks.

Run from project root:
    python3 scripts/ingest_missing_pages.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest_missing_pages")

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
PDF_PATH = _REPO_ROOT / "Book" / "IntroductoryBusinessStatistics-OP.pdf"

# Pages known to be missing from ingestion (1-indexed)
MISSING_PAGES = [52, 109, 111, 130, 131, 132, 210, 248, 285, 286, 313, 314,
                 319, 320, 321, 323, 340, 388, 558, 586, 587, 602]

try:
    import fitz
except ImportError:
    sys.exit("PyMuPDF not installed.")

try:
    import httpx
except ImportError:
    sys.exit("httpx not installed.")

try:
    from pymongo import MongoClient
except ImportError:
    sys.exit("pymongo not installed.")


_CHAPTER_RE = re.compile(
    r"Chapter\s+(\d{1,2})\s*[|]\s*([^\n]{5,80})", re.IGNORECASE
)


def _guess_chapter(text: str, page_num: int, col) -> tuple[int, str]:
    """Best-effort chapter detection from page text or adjacent chunks."""
    m = _CHAPTER_RE.search(text)
    if m:
        return int(m.group(1)), m.group(2).strip()

    # Look at adjacent chunks in MongoDB for chapter context
    nearby = col.find_one(
        {"book_id": BOOK_ID, "page_start": {"$lte": page_num + 2}, "chapter_num": {"$gt": 0}},
        sort=[("page_start", -1)]
    )
    if nearby:
        return nearby.get("chapter_num", 0), nearby.get("chapter_title", "Unknown")
    return 0, "Unknown"


async def _embed(text: str, http: httpx.AsyncClient) -> list[float]:
    if not GEMINI_API_KEY or not text.strip():
        return []
    payload = {
        "model": f"models/{GEMINI_EMBED_MODEL}",
        "content": {"parts": [{"text": text[:2048]}]},
        "taskType": "SEMANTIC_SIMILARITY",
        "outputDimensionality": 768,
    }
    for attempt in range(5):
        resp = await http.post(
            f"{GEMINI_BASE}/models/{GEMINI_EMBED_MODEL}:embedContent",
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=30,
        )
        if resp.status_code == 429:
            wait = 15 * (2 ** min(attempt, 3))
            log.warning(f"Rate limited, waiting {wait}s…")
            await asyncio.sleep(wait)
            continue
        if resp.status_code in {500, 503}:
            await asyncio.sleep(5 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()["embedding"]["values"]
    return []


def _describe_page_for_embed(text: str, chapter_num: int, chapter_title: str) -> str:
    parts = [f"Chapter {chapter_num}: {chapter_title}", text.strip()]
    return "\n\n".join(p for p in parts if p.strip())


async def main():
    mongo_url = MONGODB_URL.replace("mongodb://mongodb:", "mongodb://localhost:")
    client = MongoClient(mongo_url, serverSelectionTimeoutMS=10000, directConnection=True)
    db = client[MONGODB_DB]
    col = db["pdf_chunks"]

    # First verify which pages are still missing (in case some were added)
    existing_pages = set()
    for chunk in col.find({"book_id": BOOK_ID}, {"page_start": 1, "page_end": 1}):
        ps = chunk.get("page_start", 0)
        pe = chunk.get("page_end", 0)
        if pe > ps + 50:
            pe = ps
        for pg in range(ps, pe + 1):
            existing_pages.add(pg)

    still_missing = [p for p in MISSING_PAGES if p not in existing_pages]
    log.info(f"Pages to ingest: {still_missing}")

    if not still_missing:
        log.info("All previously missing pages are now covered!")
        client.close()
        return

    doc = fitz.open(str(PDF_PATH))
    inserted = 0

    async with httpx.AsyncClient() as http:
        # Group consecutive missing pages into batches (within 2 pages of each other)
        groups = []
        current_group = [still_missing[0]]
        for pg in still_missing[1:]:
            if pg - current_group[-1] <= 3:
                current_group.append(pg)
            else:
                groups.append(current_group)
                current_group = [pg]
        groups.append(current_group)

        log.info(f"Processing {len(groups)} page groups: {groups}")

        for group in groups:
            pg_start = group[0]
            pg_end = group[-1]

            # Collect all text from this group of pages
            all_text_parts = []
            has_images = False
            has_tables = False

            for pg_num in group:
                page = doc[pg_num - 1]  # 0-indexed
                try:
                    text = page.get_text("text").strip()
                except Exception:
                    text = ""

                if text:
                    all_text_parts.append(text)

                # Check for images
                if page.get_images(full=True):
                    has_images = True

                # Check for tables
                try:
                    if page.find_tables().tables:
                        has_tables = True
                except Exception:
                    pass

            combined_text = "\n\n".join(all_text_parts).strip()
            if not combined_text or len(combined_text) < 20:
                log.info(f"  Pages {pg_start}-{pg_end}: skipping (text too short: {len(combined_text)} chars)")
                continue

            ch_num, ch_title = _guess_chapter(combined_text, pg_start, col)
            log.info(f"  Pages {pg_start}-{pg_end}: {len(combined_text)} chars, ch{ch_num} '{ch_title[:40]}'")

            embed_text = _describe_page_for_embed(combined_text, ch_num, ch_title)
            emb = await _embed(embed_text, http)
            if not emb:
                log.warning(f"  Pages {pg_start}-{pg_end}: embedding failed, inserting without embedding")

            doc_rec = {
                "book_id": BOOK_ID,
                "chapter_num": ch_num,
                "chapter_title": ch_title,
                "section_title": "Supplemental Content",
                "topic_tag": ch_title or "Supplemental",
                "text": combined_text,
                "image_texts": [],
                "table_texts": [],
                "math_text": "",
                "page_start": pg_start,
                "page_end": pg_end,
                "has_images": has_images,
                "has_tables": has_tables,
                "has_math": False,
                "has_formula": False,
                "has_example": False,
                "teaching_density": 0.0,
                "key_terms": [],
                "graph_page_nums": [],
                "math_page_nums": [],
                "embedding": emb,
                "created_at": datetime.now(timezone.utc),
                "supplemental": True,
            }
            col.insert_one(doc_rec)
            inserted += 1
            log.info(f"  Inserted chunk for pages {pg_start}-{pg_end}")

            await asyncio.sleep(2)  # Rate limit courtesy

    doc.close()
    client.close()

    log.info("=" * 60)
    log.info(f"DONE — Inserted {inserted} supplemental chunks for {len(still_missing)} missing pages")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
