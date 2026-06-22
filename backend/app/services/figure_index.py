"""
figure_index.py — specialist RAG index for figures/charts (Phase 2 of MULTI_RAG_DESIGN).

One document per figure, built from the vision descriptions already stored on
pdf_chunks during ingestion (no new vision calls — the descriptions are reused;
the only new LLM spend is a small batched classification of figure kind +
axis summary, cached by content hash). Built on worker-vision via
build_figure_index_task.

L4 (Analyze) question generation retrieves these so data-interpretation
questions reference actual charts from the book instead of hoping a chart
description survives inside a prose chunk's embedding.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from app.core.config import settings
from app.services.mongo_vector_store import (
    CHUNKS_COLLECTION,
    FIGURE_COLLECTION,
    FIGURE_INDEX_NAME,
    INDEX_JOBS_COLLECTION,
    _get_collection,
    vector_search,
)

logger = logging.getLogger(__name__)

BUILD_CACHE_COLLECTION = "index_build_cache"

FIGURE_KINDS = ("histogram", "bar", "scatter", "boxplot", "line", "pie", "table-figure", "diagram", "other")

_CAPTION_RE = re.compile(r"^(Figure|Fig\.?)\s+[\d.]+.{0,160}", re.IGNORECASE | re.MULTILINE)
_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)

_CLASSIFY_PROMPT = """\
You are indexing chart/figure descriptions extracted from a statistics textbook.

For EACH numbered description below, produce:
  - figure_kind: one of {kinds}
  - axis_summary: ONE line naming the axes/variables and the visible trend or shape
    (e.g. "x: exam score, y: frequency — right-skewed with a peak near 70")

Return ONLY a JSON array with one object per description, in the same order,
with keys: i (the number), figure_kind, axis_summary.

DESCRIPTIONS:
{descriptions_block}
"""


# ── Extraction from stored chunks ───────────────────────────────────────────────

def split_figures(image_texts: list, chunk_text: str = "", min_chars: int = 20) -> list[dict]:
    """One entry per stored vision description: [{"description", "caption"}]."""
    caption = extract_caption(chunk_text)
    seen: set[str] = set()
    out: list[dict] = []
    for desc in image_texts or []:
        desc = (desc or "").strip()
        norm = _normalise(desc)
        if len(desc) < min_chars or norm in seen:
            continue
        seen.add(norm)
        out.append({"description": desc, "caption": caption})
    return out


def extract_caption(chunk_text: str) -> str:
    """First 'Figure N.M …' line in the chunk's prose, if any."""
    m = _CAPTION_RE.search(chunk_text or "")
    return m.group(0).strip() if m else ""


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()[:300]


def figure_doc_id(book_hash: str, parent_chunk_id: str, description: str) -> str:
    key = f"{book_hash}:{parent_chunk_id}:{_normalise(description)}"
    return hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:24]


def _cache_key(description: str) -> str:
    return "figure:" + hashlib.sha1(_normalise(description).encode()).hexdigest()


# ── LLM classification (batched + cached) ──────────────────────────────────────

def parse_classification(raw: str, batch_size: int) -> dict[int, dict]:
    """Parse the LLM output → {figure_number: {figure_kind, axis_summary}}."""
    m = _JSON_RE.search(raw or "")
    if not m:
        return {}
    try:
        items = json.loads(m.group())
    except json.JSONDecodeError:
        return {}
    if not isinstance(items, list):
        return {}
    out: dict[int, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i", 0))
        except (TypeError, ValueError):
            continue
        if not (1 <= i <= batch_size):
            continue
        kind = str(item.get("figure_kind", "")).strip().lower()
        if kind not in FIGURE_KINDS:
            kind = "other"
        out[i] = {
            "figure_kind": kind,
            "axis_summary": str(item.get("axis_summary", "")).strip()[:300],
        }
    return out


async def _classify_batch(entries: list[dict], cache_col) -> list[dict]:
    from app.services.llm_service import llm_service

    todo: list[int] = []
    for idx, e in enumerate(entries):
        cached = None
        if cache_col is not None:
            try:
                cached = await cache_col.find_one({"_id": _cache_key(e["description"])})
            except Exception:
                pass
        if cached:
            e["figure_kind"] = cached.get("figure_kind", "other")
            e["axis_summary"] = cached.get("axis_summary", "")
        else:
            todo.append(idx)

    if todo:
        block = "\n".join(
            f"{n}. {entries[i]['description'][:500]}"
            for n, i in enumerate(todo, 1)
        )
        parsed: dict[int, dict] = {}
        try:
            raw = await llm_service.generate(_CLASSIFY_PROMPT.format(
                kinds=", ".join(FIGURE_KINDS), descriptions_block=block,
            ))
            parsed = parse_classification(raw, batch_size=len(todo))
        except Exception as exc:
            logger.warning(f"figure classification LLM call failed (using fallbacks): {exc}")

        for n, i in enumerate(todo, 1):
            e = entries[i]
            got = parsed.get(n, {})
            e["figure_kind"] = got.get("figure_kind", "other")
            e["axis_summary"] = got.get("axis_summary", "")
            if cache_col is not None:
                try:
                    await cache_col.replace_one(
                        {"_id": _cache_key(e["description"])},
                        {"_id": _cache_key(e["description"]),
                         "figure_kind": e["figure_kind"],
                         "axis_summary": e["axis_summary"],
                         "created_at": datetime.now(timezone.utc)},
                        upsert=True,
                    )
                except Exception:
                    pass
    return entries


def embedding_text(entry: dict) -> str:
    parts = [entry.get("figure_kind") or "figure"]
    if entry.get("caption"):
        parts.append(entry["caption"])
    if entry.get("axis_summary"):
        parts.append(entry["axis_summary"])
    parts.append((entry.get("description") or "")[:600])
    return " — ".join(p for p in parts if p)


# ── Builder (runs on worker-vision) ────────────────────────────────────────────

async def build_figure_index(book_id: str) -> dict:
    """Build (or rebuild) the figure index for one book. Idempotent."""
    from app.services.llm_service import llm_service

    jobs_col = await _get_collection(INDEX_JOBS_COLLECTION)
    job_id = f"figure:{book_id}"
    now = datetime.now(timezone.utc)
    await jobs_col.replace_one(
        {"_id": job_id},
        {"_id": job_id, "index": "figure", "book_id": book_id,
         "status": "processing", "started_at": now, "finished_at": None, "error": None},
        upsert=True,
    )

    try:
        chunks_col = await _get_collection(CHUNKS_COLLECTION)
        cursor = chunks_col.find(
            {"book_id": book_id, "image_texts.0": {"$exists": True}},
            {"text": 1, "image_texts": 1, "book_hash": 1, "chapter_num": 1,
             "chapter_title": 1, "section_title": 1, "page_start": 1},
        )

        entries: list[dict] = []
        seen_ids: set[str] = set()
        async for chunk in cursor:
            chunk_id = str(chunk["_id"])
            for f in split_figures(chunk.get("image_texts", []), chunk.get("text", "")):
                doc_id = figure_doc_id(chunk.get("book_hash") or book_id, chunk_id, f["description"])
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)
                f.update({
                    "_id": doc_id, "book_id": book_id,
                    "book_hash": chunk.get("book_hash"),
                    "parent_chunk_id": chunk_id,
                    "chapter_num": chunk.get("chapter_num", 0),
                    "chapter_title": chunk.get("chapter_title", ""),
                    "section_title": chunk.get("section_title", ""),
                    "page": chunk.get("page_start", 0),
                })
                entries.append(f)

        if not entries:
            await jobs_col.update_one({"_id": job_id}, {"$set": {
                "status": "done", "figures": 0, "finished_at": datetime.now(timezone.utc)}})
            return {"book_id": book_id, "figures": 0}

        cache_col = None
        try:
            cache_col = await _get_collection(BUILD_CACHE_COLLECTION)
        except Exception:
            pass

        batch = max(1, settings.INDEX_BUILD_BATCH_SIZE)
        for i in range(0, len(entries), batch):
            await _classify_batch(entries[i:i + batch], cache_col)

        texts = [embedding_text(e) for e in entries]
        embeddings: list[list[float]] = []
        for i in range(0, len(texts), max(1, settings.EMBEDDING_BATCH_SIZE)):
            chunk_texts = texts[i:i + max(1, settings.EMBEDDING_BATCH_SIZE)]
            try:
                embeddings.extend(await llm_service.embed_batch(chunk_texts))
            except Exception:
                for t in chunk_texts:
                    try:
                        embeddings.append(await llm_service.embed(t))
                    except Exception:
                        embeddings.append([])

        col = await _get_collection(FIGURE_COLLECTION)
        stored = 0
        for e, emb in zip(entries, embeddings):
            if not emb:
                continue
            doc = {
                "_id": e["_id"], "book_id": e["book_id"], "book_hash": e["book_hash"],
                "parent_chunk_id": e["parent_chunk_id"],
                "chapter_num": e["chapter_num"], "chapter_title": e["chapter_title"],
                "section_title": e["section_title"], "page": e["page"],
                "figure_kind": e["figure_kind"], "description": e["description"],
                "axis_summary": e["axis_summary"], "caption": e["caption"],
                "embedding": emb, "created_at": datetime.now(timezone.utc),
            }
            try:
                await col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                stored += 1
            except Exception as exc:
                logger.warning(f"figure_index upsert failed for {doc['_id']}: {exc}")

        try:
            await col.create_index("parent_chunk_id")
            await col.create_index("book_id")
        except Exception:
            pass

        await jobs_col.update_one({"_id": job_id}, {"$set": {
            "status": "done", "figures": stored, "finished_at": datetime.now(timezone.utc)}})
        logger.info(f"figure_index: built {stored} figure docs for book '{book_id}'")
        return {"book_id": book_id, "figures": stored}

    except Exception as exc:
        await jobs_col.update_one({"_id": job_id}, {"$set": {
            "status": "failed", "error": str(exc)[:500],
            "finished_at": datetime.now(timezone.utc)}})
        raise


# ── Retrieval + prompt rendering ───────────────────────────────────────────────

async def retrieve_figures(
    query_embedding: list[float],
    book_id: str | None = None,
    chapter_num: int | None = None,
    k: int = 3,
) -> list[dict]:
    if not settings.FIGURE_INDEX_ENABLED:
        return []
    filters = {"chapter_num": chapter_num} if chapter_num is not None else None
    return await vector_search(
        query_embedding, k=k, book_id=book_id, filters=filters,
        collection_name=FIGURE_COLLECTION, index_name=FIGURE_INDEX_NAME,
    )


def render_figures_block(figures: list[dict]) -> str:
    """Render retrieved figure docs as a prompt section. Empty string if none."""
    if not figures:
        return ""
    lines = ["FIGURES FROM THE TEXTBOOK (write data-interpretation questions that reference these):"]
    for f in figures:
        kind = f.get("figure_kind", "figure")
        caption = f.get("caption") or f"{kind} (Ch{f.get('chapter_num', '?')}, p.{f.get('page', '?')})"
        axis = f.get("axis_summary") or ""
        desc = (f.get("description") or "")[:280]
        line = f"- [{kind}] {caption}"
        if axis:
            line += f" — {axis}"
        lines.append(line)
        lines.append(f"  Description: {desc}")
    return "\n".join(lines)


async def delete_figure_index(book_id: str) -> int:
    try:
        col = await _get_collection(FIGURE_COLLECTION)
        result = await col.delete_many({"book_id": book_id})
        jobs_col = await _get_collection(INDEX_JOBS_COLLECTION)
        await jobs_col.delete_one({"_id": f"figure:{book_id}"})
        return result.deleted_count
    except Exception as exc:
        logger.warning(f"delete_figure_index failed (non-fatal): {exc}")
        return 0


async def figure_index_status() -> list[dict]:
    out: list[dict] = []
    try:
        jobs_col = await _get_collection(INDEX_JOBS_COLLECTION)
        col = await _get_collection(FIGURE_COLLECTION)
        async for job in jobs_col.find({"index": "figure"}):
            book_id = job.get("book_id")
            count = await col.count_documents({"book_id": book_id})
            out.append({
                "book_id": book_id, "status": job.get("status"),
                "figures": count, "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"), "error": job.get("error"),
            })
    except Exception as exc:
        logger.warning(f"figure_index_status failed: {exc}")
    return out
