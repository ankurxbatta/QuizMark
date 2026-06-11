"""
table_index.py — specialist RAG index for tables (Phase 2 of MULTI_RAG_DESIGN).

One document per extracted table, built from the markdown tables already stored
in pdf_chunks.table_texts. The only LLM spend is one short batched summary per
table, cached by content hash. Built on worker-clean via build_table_index_task
(CPU-light queue with spare capacity).

L4 (Analyze) question generation retrieves these so questions can ask students
to read and interpret actual tables from the book.
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
    INDEX_JOBS_COLLECTION,
    TABLE_COLLECTION,
    TABLE_INDEX_NAME,
    _get_collection,
    vector_search,
)

logger = logging.getLogger(__name__)

BUILD_CACHE_COLLECTION = "index_build_cache"

_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)

_SUMMARISE_PROMPT = """\
You are indexing tables extracted from a statistics textbook.

For EACH numbered table below, produce:
  - table_summary: ONE line saying what the table shows and what its key columns are
    (e.g. "frequency distribution of exam scores in five class intervals")

Return ONLY a JSON array with one object per table, in the same order,
with keys: i (the table number), table_summary.

TABLES:
{tables_block}
"""


# ── Extraction from stored chunks ───────────────────────────────────────────────

def split_tables(table_texts: list, min_chars: int = 30) -> list[dict]:
    """One entry per stored markdown table: [{"table_markdown", "headers"}]."""
    seen: set[str] = set()
    out: list[dict] = []
    for table in table_texts or []:
        table = (table or "").strip()
        norm = _normalise(table)
        if len(table) < min_chars or norm in seen:
            continue
        seen.add(norm)
        out.append({"table_markdown": table, "headers": extract_headers(table)})
    return out


def extract_headers(table_markdown: str) -> list[str]:
    """Column headers from the first markdown row, e.g. '| a | b |' → ['a', 'b']."""
    for line in table_markdown.splitlines():
        line = line.strip()
        if not line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        cells = [c for c in cells if c and not set(c) <= {"-", ":", " "}]
        return cells[:10]
    return []


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()[:300]


def table_doc_id(book_hash: str, parent_chunk_id: str, table_markdown: str) -> str:
    key = f"{book_hash}:{parent_chunk_id}:{_normalise(table_markdown)}"
    return hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:24]


def _cache_key(table_markdown: str) -> str:
    return "table:" + hashlib.sha1(_normalise(table_markdown).encode()).hexdigest()


# ── LLM summarisation (batched + cached) ───────────────────────────────────────

def parse_summaries(raw: str, batch_size: int) -> dict[int, str]:
    """Parse the LLM output → {table_number: table_summary}."""
    m = _JSON_RE.search(raw or "")
    if not m:
        return {}
    try:
        items = json.loads(m.group())
    except json.JSONDecodeError:
        return {}
    if not isinstance(items, list):
        return {}
    out: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i", 0))
        except (TypeError, ValueError):
            continue
        if not (1 <= i <= batch_size):
            continue
        summary = str(item.get("table_summary", "")).strip()[:300]
        if summary:
            out[i] = summary
    return out


async def _summarise_batch(entries: list[dict], cache_col) -> list[dict]:
    from app.services.llm_service import llm_service

    todo: list[int] = []
    for idx, e in enumerate(entries):
        cached = None
        if cache_col is not None:
            try:
                cached = await cache_col.find_one({"_id": _cache_key(e["table_markdown"])})
            except Exception:
                pass
        if cached:
            e["table_summary"] = cached.get("table_summary", "")
        else:
            todo.append(idx)

    if todo:
        block = "\n".join(
            f"{n}. {entries[i]['table_markdown'][:400]}"
            for n, i in enumerate(todo, 1)
        )
        parsed: dict[int, str] = {}
        try:
            raw = await llm_service.generate(_SUMMARISE_PROMPT.format(tables_block=block))
            parsed = parse_summaries(raw, batch_size=len(todo))
        except Exception as exc:
            logger.warning(f"table summary LLM call failed (using fallbacks): {exc}")

        for n, i in enumerate(todo, 1):
            e = entries[i]
            e["table_summary"] = parsed.get(n, "") or ", ".join(e["headers"])[:200]
            if cache_col is not None:
                try:
                    await cache_col.replace_one(
                        {"_id": _cache_key(e["table_markdown"])},
                        {"_id": _cache_key(e["table_markdown"]),
                         "table_summary": e["table_summary"],
                         "created_at": datetime.now(timezone.utc)},
                        upsert=True,
                    )
                except Exception:
                    pass
    return entries


def embedding_text(entry: dict) -> str:
    parts = []
    if entry.get("table_summary"):
        parts.append(entry["table_summary"])
    if entry.get("headers"):
        parts.append("headers: " + ", ".join(entry["headers"]))
    first_rows = "\n".join(entry.get("table_markdown", "").splitlines()[:4])
    parts.append(first_rows)
    return " — ".join(p for p in parts if p)


# ── Builder (runs on worker-clean) ─────────────────────────────────────────────

async def build_table_index(book_id: str) -> dict:
    """Build (or rebuild) the table index for one book. Idempotent."""
    from app.services.llm_service import llm_service

    jobs_col = await _get_collection(INDEX_JOBS_COLLECTION)
    job_id = f"table:{book_id}"
    now = datetime.now(timezone.utc)
    await jobs_col.replace_one(
        {"_id": job_id},
        {"_id": job_id, "index": "table", "book_id": book_id,
         "status": "processing", "started_at": now, "finished_at": None, "error": None},
        upsert=True,
    )

    try:
        chunks_col = await _get_collection(CHUNKS_COLLECTION)
        cursor = chunks_col.find(
            {"book_id": book_id, "table_texts.0": {"$exists": True}},
            {"table_texts": 1, "book_hash": 1, "chapter_num": 1,
             "chapter_title": 1, "section_title": 1, "page_start": 1},
        )

        entries: list[dict] = []
        seen_ids: set[str] = set()
        async for chunk in cursor:
            chunk_id = str(chunk["_id"])
            for t in split_tables(chunk.get("table_texts", [])):
                doc_id = table_doc_id(chunk.get("book_hash") or book_id, chunk_id, t["table_markdown"])
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)
                t.update({
                    "_id": doc_id, "book_id": book_id,
                    "book_hash": chunk.get("book_hash"),
                    "parent_chunk_id": chunk_id,
                    "chapter_num": chunk.get("chapter_num", 0),
                    "chapter_title": chunk.get("chapter_title", ""),
                    "section_title": chunk.get("section_title", ""),
                    "page": chunk.get("page_start", 0),
                })
                entries.append(t)

        if not entries:
            await jobs_col.update_one({"_id": job_id}, {"$set": {
                "status": "done", "tables": 0, "finished_at": datetime.now(timezone.utc)}})
            return {"book_id": book_id, "tables": 0}

        cache_col = None
        try:
            cache_col = await _get_collection(BUILD_CACHE_COLLECTION)
        except Exception:
            pass

        batch = max(1, settings.INDEX_BUILD_BATCH_SIZE)
        for i in range(0, len(entries), batch):
            await _summarise_batch(entries[i:i + batch], cache_col)

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

        col = await _get_collection(TABLE_COLLECTION)
        stored = 0
        for e, emb in zip(entries, embeddings):
            if not emb:
                continue
            doc = {
                "_id": e["_id"], "book_id": e["book_id"], "book_hash": e["book_hash"],
                "parent_chunk_id": e["parent_chunk_id"],
                "chapter_num": e["chapter_num"], "chapter_title": e["chapter_title"],
                "section_title": e["section_title"], "page": e["page"],
                "table_markdown": e["table_markdown"][:6000],
                "table_summary": e["table_summary"], "headers": e["headers"],
                "embedding": emb, "created_at": datetime.now(timezone.utc),
            }
            try:
                await col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                stored += 1
            except Exception as exc:
                logger.warning(f"table_index upsert failed for {doc['_id']}: {exc}")

        try:
            await col.create_index("parent_chunk_id")
            await col.create_index("book_id")
        except Exception:
            pass

        await jobs_col.update_one({"_id": job_id}, {"$set": {
            "status": "done", "tables": stored, "finished_at": datetime.now(timezone.utc)}})
        logger.info(f"table_index: built {stored} table docs for book '{book_id}'")
        return {"book_id": book_id, "tables": stored}

    except Exception as exc:
        await jobs_col.update_one({"_id": job_id}, {"$set": {
            "status": "failed", "error": str(exc)[:500],
            "finished_at": datetime.now(timezone.utc)}})
        raise


# ── Retrieval + prompt rendering ───────────────────────────────────────────────

async def retrieve_tables(
    query_embedding: list[float],
    book_id: str | None = None,
    k: int = 2,
) -> list[dict]:
    if not settings.TABLE_INDEX_ENABLED:
        return []
    return await vector_search(
        query_embedding, k=k, book_id=book_id,
        collection_name=TABLE_COLLECTION, index_name=TABLE_INDEX_NAME,
    )


def render_tables_block(tables: list[dict]) -> str:
    """Render retrieved table docs as a prompt section. Empty string if none."""
    if not tables:
        return ""
    lines = ["TABLES FROM THE TEXTBOOK (write questions that require reading these):"]
    for t in tables:
        summary = t.get("table_summary") or "table"
        lines.append(f"- {summary} (Ch{t.get('chapter_num', '?')}, p.{t.get('page', '?')})")
        table_md = (t.get("table_markdown") or "").strip()
        # Include up to the first 8 rows so the model can build real questions on it
        lines.append("\n".join(table_md.splitlines()[:8]))
    return "\n".join(lines)


async def delete_table_index(book_id: str) -> int:
    try:
        col = await _get_collection(TABLE_COLLECTION)
        result = await col.delete_many({"book_id": book_id})
        jobs_col = await _get_collection(INDEX_JOBS_COLLECTION)
        await jobs_col.delete_one({"_id": f"table:{book_id}"})
        return result.deleted_count
    except Exception as exc:
        logger.warning(f"delete_table_index failed (non-fatal): {exc}")
        return 0


async def table_index_status() -> list[dict]:
    out: list[dict] = []
    try:
        jobs_col = await _get_collection(INDEX_JOBS_COLLECTION)
        col = await _get_collection(TABLE_COLLECTION)
        async for job in jobs_col.find({"index": "table"}):
            book_id = job.get("book_id")
            count = await col.count_documents({"book_id": book_id})
            out.append({
                "book_id": book_id, "status": job.get("status"),
                "tables": count, "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"), "error": job.get("error"),
            })
    except Exception as exc:
        logger.warning(f"table_index_status failed: {exc}")
    return out
