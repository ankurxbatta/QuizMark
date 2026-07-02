#!/usr/bin/env python3
"""
verify_ingestion.py — Verify accuracy and completeness of PDF ingestion in MongoDB.

Run from project root:
    python3 scripts/verify_ingestion.py

Checks:
1. All PDF pages are represented in chunks (no silent gaps)
2. All chunks have valid embeddings (768-dim)
3. Chapter structure is correct (sequential, reasonable count)
4. Text quality (no empty/corrupt chunks)
5. Math/table/image detection coverage
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("verify_ingestion")

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
BOOK_ID = "IntroductoryBusinessStatistics-OP"
PDF_PATH = _REPO_ROOT / "Book" / "IntroductoryBusinessStatistics-OP.pdf"


try:
    from pymongo import MongoClient
except ImportError:
    sys.exit("pymongo not installed.")

try:
    import fitz
except ImportError:
    sys.exit("PyMuPDF not installed.")


def main():
    mongo_url = MONGODB_URL.replace("mongodb://mongodb:", "mongodb://localhost:")
    client = MongoClient(mongo_url, serverSelectionTimeoutMS=10000, directConnection=True)
    db = client[MONGODB_DB]
    col = db["pdf_chunks"]

    # ── PDF ground truth ────────────────────────────────────────────────────────
    if not PDF_PATH.exists():
        log.warning(f"PDF not found at {PDF_PATH}, skipping page coverage check")
        pdf_pages = None
    else:
        doc = fitz.open(str(PDF_PATH))
        pdf_pages = doc.page_count  # 0-indexed, so pages 0..pdf_pages-1 => 1-indexed 1..pdf_pages
        doc.close()
        log.info(f"PDF: {pdf_pages} pages")

    # ── Fetch all chunks ────────────────────────────────────────────────────────
    chunks = list(col.find(
        {"book_id": BOOK_ID},
        {
            "_id": 1, "page_start": 1, "page_end": 1,
            "chapter_num": 1, "chapter_title": 1, "section_title": 1,
            "text": 1, "embedding": 1, "has_math": 1, "has_tables": 1,
            "has_images": 1, "math_text": 1, "table_texts": 1
        }
    ).sort("page_start", 1))

    total = len(chunks)
    log.info(f"Total chunks in MongoDB: {total}")

    issues = []

    # ── Check 1: Embedding coverage ─────────────────────────────────────────────
    no_embed = [c for c in chunks if not c.get("embedding")]
    wrong_dim = [c for c in chunks if c.get("embedding") and len(c["embedding"]) != 768]
    log.info(f"[Embeddings] Missing: {len(no_embed)}, Wrong dim: {len(wrong_dim)}, OK: {total - len(no_embed) - len(wrong_dim)}")
    if no_embed:
        issues.append(f"FAIL: {len(no_embed)} chunks missing embeddings (pages {sorted({c.get('page_start') for c in no_embed})[:5]}...)")
    if wrong_dim:
        issues.append(f"FAIL: {len(wrong_dim)} chunks have wrong embedding dimension")

    # ── Check 2: Page coverage ───────────────────────────────────────────────────
    covered_pages = set()
    for c in chunks:
        ps = c.get("page_start", 0)
        pe = c.get("page_end", 0)
        if pe > ps + 50:
            pe = ps  # sanity clamp for end-flush artifacts
        for pg in range(ps, pe + 1):
            covered_pages.add(pg)

    if pdf_pages:
        # Pages 1..pdf_pages in 1-indexed (script uses page.number + 1)
        expected_pages = set(range(1, pdf_pages + 1))
        missing_pages = expected_pages - covered_pages
        extra_pages = covered_pages - expected_pages

        # Skip the last-flush artifact (page_end set to MAX_PAGES=700)
        extra_pages_real = {p for p in extra_pages if p <= pdf_pages + 5}

        log.info(f"[Page Coverage] Expected: {pdf_pages} pages (1-{pdf_pages})")
        log.info(f"[Page Coverage] Covered: {len(covered_pages & expected_pages)} pages")
        log.info(f"[Page Coverage] Missing: {len(missing_pages)} pages: {sorted(missing_pages)[:30]}")
        log.info(f"[Page Coverage] Extra (beyond PDF): {len(extra_pages_real)} pages: {sorted(extra_pages_real)[:30]}")

        # Check if missing pages are blank in PDF
        doc = fitz.open(str(PDF_PATH))
        truly_missing = []
        blank_pages = []
        for pg in sorted(missing_pages):
            page = doc[pg - 1]  # convert to 0-indexed
            text = page.get_text("text").strip()
            if len(text) < 20:
                blank_pages.append(pg)
            else:
                truly_missing.append(pg)
        doc.close()

        log.info(f"[Page Coverage] Blank/empty in PDF: {len(blank_pages)} pages")
        log.info(f"[Page Coverage] Truly missing content: {len(truly_missing)} pages: {truly_missing[:20]}")

        if truly_missing:
            issues.append(f"FAIL: {len(truly_missing)} non-blank pages not covered: {truly_missing[:10]}")
        else:
            log.info("[Page Coverage] PASS — all missing pages are blank in PDF")

    # ── Check 3: Text quality ───────────────────────────────────────────────────
    empty_text = [c for c in chunks if len(c.get("text", "").strip()) < 100]
    short_text = [c for c in chunks if 100 <= len(c.get("text", "").strip()) < 300]
    avg_len = sum(len(c.get("text", "")) for c in chunks) / max(total, 1)
    log.info(f"[Text Quality] Avg length: {avg_len:.0f} chars, Empty(<100): {len(empty_text)}, Short(100-300): {len(short_text)}")
    if empty_text:
        issues.append(f"WARN: {len(empty_text)} chunks have very short text (<100 chars)")

    # ── Check 4: Chapter structure ───────────────────────────────────────────────
    chap_counts: dict[int, int] = {}
    for c in chunks:
        ch = c.get("chapter_num", 0)
        chap_counts[ch] = chap_counts.get(ch, 0) + 1

    chapters_found = sorted(ch for ch in chap_counts if ch > 0)
    log.info(f"[Chapters] Found: {chapters_found}")
    log.info(f"[Chapters] Chunk counts: { {k: v for k, v in sorted(chap_counts.items())} }")

    if not chapters_found:
        issues.append("FAIL: No chapters detected!")
    elif chapters_found[-1] < 10:
        issues.append(f"WARN: Only {len(chapters_found)} chapters found (expected ~13 for this book)")

    # Check for reasonable chapter progression (no skipped chapters)
    expected_chapters = set(range(1, chapters_found[-1] + 1)) if chapters_found else set()
    missing_chapters = expected_chapters - set(chapters_found)
    if missing_chapters:
        issues.append(f"WARN: Missing chapters: {missing_chapters}")

    # ── Check 5: Content richness ────────────────────────────────────────────────
    has_math = sum(1 for c in chunks if c.get("has_math"))
    has_math_text = sum(1 for c in chunks if c.get("math_text"))
    has_tables = sum(1 for c in chunks if c.get("has_tables"))
    has_images = sum(1 for c in chunks if c.get("has_images"))
    log.info(f"[Content] Math font: {has_math} ({100*has_math//total}%), Math LaTeX: {has_math_text}, Tables: {has_tables}, Images: {has_images}")

    # ── Summary ──────────────────────────────────────────────────────────────────
    log.info("=" * 60)
    if not issues:
        log.info("VERIFICATION PASSED — All checks passed!")
    else:
        log.info(f"VERIFICATION COMPLETE — {len(issues)} issue(s) found:")
        for i, issue in enumerate(issues, 1):
            log.info(f"  {i}. {issue}")

    # Final stats
    embed_ok = total - len(no_embed) - len(wrong_dim)
    log.info("\nSummary:")
    log.info(f"  Chunks: {total}")
    log.info(f"  Embeddings OK: {embed_ok}/{total} ({100*embed_ok//max(total,1)}%)")
    log.info(f"  Chapters: {len(chapters_found)} ({min(chapters_found) if chapters_found else '?'}-{max(chapters_found) if chapters_found else '?'})")
    log.info(f"  Avg text: {avg_len:.0f} chars/chunk")
    log.info("=" * 60)

    client.close()
    return len(issues)


if __name__ == "__main__":
    sys.exit(main())
