"""
chunk_validator.py — validation worker that runs on every chunk before it is
inserted into MongoDB.

Two layers:
  1. Structural validation (all chunks, free):
       strip/normalise text fields, dedupe lists, fix inverted page ranges,
       clamp runaway field sizes.
  2. Math repair (formula chunks, LLM):
       PDF text extraction loses layout-encoded operators — a stacked fraction
       like ∑fm over ∑f comes out as "∑fm ∑f" with the divide bar gone. An LLM
       pass restores the lost operators so the database stores "∑fm / ∑f".
       Results are cached in `validation_cache` by content hash, so resumed or
       re-uploaded books never pay for the same correction twice.

Repairs are conservative: output that parses badly or drifts too far in length
is rejected and the original text is kept. Failures never block ingestion.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from app.core.config import settings

logger = logging.getLogger(__name__)

VALIDATION_CACHE_COLLECTION = "validation_cache"

_MATH_REPAIR_PROMPT = """\
You are a data validator for textbook content extracted from a PDF.

PDF extraction loses layout-encoded math operators. Typical damage:
  - stacked fractions lose the divide bar: "∑fm ∑f" should be "∑fm / ∑f"
  - superscripts/subscripts collapse: "x2" may mean "x^2", "xi" may mean "x_i"
  - square roots, equals signs and minus signs sometimes vanish

Repair ONLY clearly damaged formulas in the text below. Rules:
  - Do NOT paraphrase, summarise, reorder, or add content.
  - Keep every word of the prose exactly as-is.
  - Only insert missing math operators where the formula is unambiguous.
  - If nothing is damaged, return the input unchanged.

Return ONLY a JSON object: {{"text": "<corrected text>", "math_text": "<corrected math_text>"}}

text:
{text}

math_text:
{math_text}
"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


# ── Content dedupe: one canonical home per piece of content ────────────────────
#   text        → prose only
#   table_texts → tables (markdown) — inline duplicates removed from text
#   math_text   → novel math only (vision LaTeX) — raw spans repeating the
#                 prose are dropped, since the equation already lives in text
# An equation inside a table therefore counts exactly once (in table_texts),
# and the embedding text becomes a concatenation of disjoint parts.

_TOKEN_RE = re.compile(r"\w+")


def _tokens(s: str) -> list[str]:
    return _TOKEN_RE.findall(s.lower())


def dedupe_chunk_content(chunk) -> None:
    """Remove extraction duplicates in place. Conservative — never empties a chunk."""
    try:
        table_tokens: set[str] = set()
        for t in getattr(chunk, "table_texts", []):
            table_tokens.update(_tokens(t))

        # 1) Drop prose lines that are really table rows (the page text extractor
        #    emits table cells as jumbled inline lines; table_texts has the clean
        #    markdown version, so the inline copy is pure duplication).
        if table_tokens and chunk.text:
            kept = []
            for line in chunk.text.splitlines():
                ltok = _tokens(line)
                if len(ltok) >= 3:
                    coverage = sum(1 for t in ltok if t in table_tokens) / len(ltok)
                    if coverage >= 0.75:
                        continue
                kept.append(line)
            new_text = "\n".join(kept).strip()
            if new_text:
                chunk.text = new_text

        # 2) Drop math_text that merely repeats math already inline in the text
        #    or table — novel representations (vision LaTeX) stay.
        math_text = getattr(chunk, "math_text", "")
        if math_text:
            base = set(_tokens(chunk.text)) | table_tokens
            mtok = _tokens(math_text)
            if mtok:
                coverage = sum(1 for t in mtok if t in base) / len(mtok)
                if coverage >= 0.85:
                    chunk.math_text = ""
    except Exception as exc:
        logger.debug(f"dedupe_chunk_content skipped: {exc}")


# ── Structural validation (all chunks) ─────────────────────────────────────────

def validate_structure(chunk) -> None:
    """Normalise a chunk in place. Cheap, deterministic, never raises."""
    try:
        chunk.text = (chunk.text or "").strip()
        chunk.math_text = (getattr(chunk, "math_text", "") or "").strip()[:4000]
        chunk.chapter_title = (chunk.chapter_title or "Unknown").strip()[:300]
        chunk.section_title = (chunk.section_title or "").strip()[:300]
        # Dedupe while preserving order; drop empties
        chunk.image_texts = list(dict.fromkeys(
            t.strip() for t in getattr(chunk, "image_texts", []) if t and t.strip()
        ))[:20]
        chunk.table_texts = list(dict.fromkeys(
            t.strip() for t in getattr(chunk, "table_texts", []) if t and t.strip()
        ))[:20]
        chunk.key_terms = list(dict.fromkeys(
            t.strip() for t in getattr(chunk, "key_terms", []) if t and t.strip()
        ))[:30]
        if chunk.page_start > chunk.page_end:
            chunk.page_start, chunk.page_end = chunk.page_end, chunk.page_start
    except Exception as exc:
        logger.debug(f"validate_structure skipped a field: {exc}")


# ── LLM math repair (formula chunks) ───────────────────────────────────────────

def _needs_math_repair(chunk) -> bool:
    return bool(
        getattr(chunk, "has_formula", False)
        or getattr(chunk, "math_text", "")
        or getattr(chunk, "has_math_font", False)
    )


def _repair_cache_key(text: str, math_text: str) -> str:
    return hashlib.sha1(f"{text}\x00{math_text}".encode("utf-8", "ignore")).hexdigest()


def _parse_repair(raw: str, orig_text: str, orig_math: str) -> tuple[str, str] | None:
    """Parse the LLM output; reject anything that drifted too far from the input."""
    m = _JSON_RE.search(raw or "")
    if not m:
        return None
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    text = str(data.get("text", "")).strip()
    math_text = str(data.get("math_text", "")).strip()
    if not text:
        return None
    # Repairs only insert operators — large length drift means the model
    # paraphrased or truncated, so keep the original.
    if not (0.7 * len(orig_text) <= len(text) <= 1.4 * len(orig_text) + 50):
        return None
    return text, math_text or orig_math


async def _repair_chunk_math(chunk, cache_col, semaphore) -> bool:
    """Repair one chunk's math via LLM (cache-first). Returns True if changed."""
    from app.services.llm_service import llm_service

    orig_text = chunk.text
    orig_math = getattr(chunk, "math_text", "")
    key = _repair_cache_key(orig_text, orig_math)

    if cache_col is not None:
        try:
            cached = await cache_col.find_one({"_id": key})
            if cached:
                chunk.text = cached.get("text", orig_text)
                chunk.math_text = cached.get("math_text", orig_math)
                return cached.get("changed", False)
        except Exception:
            pass

    async with semaphore:
        try:
            prompt = _MATH_REPAIR_PROMPT.format(
                text=orig_text[:3500], math_text=orig_math[:1000]
            )
            raw = await llm_service.generate(prompt)
        except Exception as exc:
            logger.debug(f"math repair LLM call failed (chunk kept as-is): {exc}")
            return False

    parsed = _parse_repair(raw, orig_text, orig_math)
    changed = False
    if parsed:
        new_text, new_math = parsed
        changed = new_text != orig_text or new_math != orig_math
        chunk.text = new_text
        chunk.math_text = new_math

    if cache_col is not None:
        try:
            await cache_col.replace_one(
                {"_id": key},
                {"_id": key, "text": chunk.text,
                 "math_text": getattr(chunk, "math_text", ""),
                 "changed": changed,
                 "created_at": datetime.now(timezone.utc)},
                upsert=True,
            )
        except Exception:
            pass
    return changed


# ── Entry point ────────────────────────────────────────────────────────────────

async def validate_chunks(chunks: list, job_id: str | None = None) -> int:
    """
    Validate (and repair where needed) a window of chunks in place, before
    embedding and DB insert. Returns the number of chunks the LLM corrected.
    Never raises — a failed validation keeps the original chunk.
    """
    if not chunks:
        return 0

    for c in chunks:
        validate_structure(c)

    if not settings.ENABLE_CHUNK_VALIDATION:
        return 0

    candidates = [c for c in chunks if _needs_math_repair(c) and c.text]
    if not candidates:
        return 0

    cache_col = None
    try:
        from app.services.mongo_vector_store import _get_collection
        cache_col = await _get_collection(VALIDATION_CACHE_COLLECTION)
    except Exception:
        pass

    semaphore = asyncio.Semaphore(max(1, settings.VALIDATION_CONCURRENCY))
    results = await asyncio.gather(
        *[_repair_chunk_math(c, cache_col, semaphore) for c in candidates],
        return_exceptions=True,
    )
    repaired = sum(1 for r in results if r is True)
    if repaired:
        logger.info(f"chunk_validator: repaired math in {repaired}/{len(candidates)} formula chunks")
    return repaired
