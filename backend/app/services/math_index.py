"""
math_index.py — specialist RAG index for formulas (Phase 1 of MULTI_RAG_DESIGN).

One document per formula occurrence, built asynchronously from chunks already
stored in pdf_chunks — content the ingestion pipeline already paid for (repaired
LaTeX from chunk_validator, prose context). The builder runs on worker-math via
build_math_index_task.

Documents carry parent_chunk_id cross-links back to pdf_chunks, deterministic
ids (rebuilds are upsert no-ops), and an embedding of
"{concept_label}: {formula_plain} — {context_sentence}" so computational
queries ("how do I calculate the sample standard deviation") land on the
formula itself rather than whatever prose chunk happens to mention it.
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
    MATH_COLLECTION,
    MATH_INDEX_NAME,
    _get_collection,
    vector_search,
)

logger = logging.getLogger(__name__)

BUILD_CACHE_COLLECTION = "index_build_cache"

# A line is formula-like if it contains an equation/operator signal and isn't prose.
_FORMULA_LINE_RE = re.compile(r"[=√∑∏∫±≤≥≠^]|\\frac|\\sqrt|\\sum|\bsqrt\(")
_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_TOKEN_RE = re.compile(r"\w+")
_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)

_ENRICH_PROMPT = """\
You are indexing formulas extracted from a statistics textbook.

For EACH numbered formula below, produce:
  - concept_label: 3–6 word name of the concept (e.g. "sample standard deviation")
  - formula_plain: the formula in linear plain text (e.g. "s = sqrt(sum((x_i - x_bar)^2) / (n - 1))")
  - variables: object mapping each symbol to its meaning (max 6 entries)

Use the context sentence to identify the concept. Do not invent formulas.

Return ONLY a JSON array with one object per formula, in the same order,
with keys: i (the formula number), concept_label, formula_plain, variables.

FORMULAS:
{formulas_block}
"""


# ── Formula extraction from stored chunks ───────────────────────────────────────

def split_formulas(math_text: str, chunk_text: str = "", max_per_chunk: int = 12) -> list[dict]:
    """
    Split a chunk's math_text (plus formula-like lines from prose) into
    individual formula entries: [{"latex": ..., "context_sentence": ...}].
    """
    seen: set[str] = set()
    out: list[dict] = []

    candidates: list[str] = []
    for line in (math_text or "").splitlines():
        line = line.strip()
        if len(line) >= 4:
            candidates.append(line)
    for line in (chunk_text or "").splitlines():
        line = line.strip()
        # Prose lines with an operator signal and few words are inline formulas.
        if len(line) >= 4 and _FORMULA_LINE_RE.search(line) and len(_WORD_RE.findall(line)) <= 6:
            candidates.append(line)

    for latex in candidates:
        norm = normalise_formula(latex)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append({
            "latex": latex,
            "context_sentence": _context_sentence(latex, chunk_text),
        })
        if len(out) >= max_per_chunk:
            break
    return out


def normalise_formula(latex: str) -> str:
    """Whitespace/case-insensitive canonical form used for dedupe and ids."""
    return re.sub(r"\s+", "", latex).lower()[:300]


def _context_sentence(formula: str, chunk_text: str, max_len: int = 240) -> str:
    """The prose sentence sharing the most tokens with the formula."""
    if not chunk_text:
        return ""
    ftok = set(_TOKEN_RE.findall(formula.lower()))
    best, best_score = "", 0
    for sentence in re.split(r"(?<=[.!?])\s+", chunk_text):
        stok = set(_TOKEN_RE.findall(sentence.lower()))
        score = len(ftok & stok)
        if score > best_score and len(sentence) > 20:
            best, best_score = sentence, score
    return best.strip()[:max_len]


def formula_doc_id(book_hash: str, parent_chunk_id: str, latex: str) -> str:
    key = f"{book_hash}:{parent_chunk_id}:{normalise_formula(latex)}"
    return hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:24]


# ── LLM enrichment (batched + cached) ──────────────────────────────────────────

def _enrich_cache_key(latex: str) -> str:
    return "math:" + hashlib.sha1(normalise_formula(latex).encode()).hexdigest()


def parse_enrichment(raw: str, batch_size: int) -> dict[int, dict]:
    """Parse the LLM enrichment output → {formula_number: {concept_label, ...}}."""
    m = _JSON_RE.search(raw or "")
    if not m:
        return {}
    try:
        items = json.loads(m.group())
    except json.JSONDecodeError:
        return {}
    out: dict[int, dict] = {}
    if not isinstance(items, list):
        return {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i", 0))
        except (TypeError, ValueError):
            continue
        if not (1 <= i <= batch_size):
            continue
        label = str(item.get("concept_label", "")).strip()[:120]
        plain = str(item.get("formula_plain", "")).strip()[:500]
        variables = item.get("variables") or {}
        if not isinstance(variables, dict):
            variables = {}
        variables = {str(k)[:20]: str(v)[:120] for k, v in list(variables.items())[:6]}
        if label or plain:
            out[i] = {"concept_label": label, "formula_plain": plain, "variables": variables}
    return out


async def _enrich_batch(entries: list[dict], cache_col) -> list[dict]:
    """Fill concept_label/formula_plain/variables for entries (cache-first)."""
    from app.services.llm_service import llm_service

    todo: list[int] = []
    for idx, e in enumerate(entries):
        cached = None
        if cache_col is not None:
            try:
                cached = await cache_col.find_one({"_id": _enrich_cache_key(e["latex"])})
            except Exception:
                pass
        if cached:
            e.update({
                "concept_label": cached.get("concept_label", ""),
                "formula_plain": cached.get("formula_plain", e["latex"]),
                "variables": cached.get("variables", {}),
            })
        else:
            todo.append(idx)

    if todo:
        block = "\n".join(
            f"{n}. {entries[i]['latex']} | context: {entries[i]['context_sentence'] or '(none)'}"
            for n, i in enumerate(todo, 1)
        )
        parsed: dict[int, dict] = {}
        try:
            raw = await llm_service.generate(_ENRICH_PROMPT.format(formulas_block=block))
            parsed = parse_enrichment(raw, batch_size=len(todo))
        except Exception as exc:
            logger.warning(f"math enrichment LLM call failed (using fallbacks): {exc}")

        for n, i in enumerate(todo, 1):
            e = entries[i]
            enriched = parsed.get(n, {})
            e["concept_label"] = enriched.get("concept_label", "")
            e["formula_plain"] = enriched.get("formula_plain") or e["latex"]
            e["variables"] = enriched.get("variables", {})
            if cache_col is not None:
                try:
                    await cache_col.replace_one(
                        {"_id": _enrich_cache_key(e["latex"])},
                        {"_id": _enrich_cache_key(e["latex"]),
                         "concept_label": e["concept_label"],
                         "formula_plain": e["formula_plain"],
                         "variables": e["variables"],
                         "created_at": datetime.now(timezone.utc)},
                        upsert=True,
                    )
                except Exception:
                    pass
    return entries


def embedding_text(entry: dict) -> str:
    label = entry.get("concept_label") or ""
    plain = entry.get("formula_plain") or entry.get("latex", "")
    ctx = entry.get("context_sentence") or ""
    return f"{label}: {plain} — {ctx}".strip(" :—")


# ── Builder (runs on worker-math) ──────────────────────────────────────────────

async def build_math_index(book_id: str) -> dict:
    """
    Build (or rebuild) the math index for one book from its stored chunks.
    Idempotent: deterministic ids + upserts. Returns build stats.
    """
    from app.services.llm_service import llm_service

    jobs_col = await _get_collection(INDEX_JOBS_COLLECTION)
    job_id = f"math:{book_id}"
    now = datetime.now(timezone.utc)
    await jobs_col.replace_one(
        {"_id": job_id},
        {"_id": job_id, "index": "math", "book_id": book_id,
         "status": "processing", "started_at": now, "finished_at": None, "error": None},
        upsert=True,
    )

    try:
        chunks_col = await _get_collection(CHUNKS_COLLECTION)
        cursor = chunks_col.find(
            {"book_id": book_id,
             "$or": [{"has_formula": True}, {"math_text": {"$nin": ["", None]}}]},
            {"text": 1, "math_text": 1, "book_hash": 1, "chapter_num": 1,
             "chapter_title": 1, "section_title": 1, "page_start": 1},
        )

        entries: list[dict] = []
        seen_ids: set[str] = set()
        async for chunk in cursor:
            chunk_id = str(chunk["_id"])
            for f in split_formulas(chunk.get("math_text", ""), chunk.get("text", "")):
                doc_id = formula_doc_id(chunk.get("book_hash") or book_id, chunk_id, f["latex"])
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)
                f.update({
                    "_id": doc_id,
                    "book_id": book_id,
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
                "status": "done", "formulas": 0, "finished_at": datetime.now(timezone.utc)}})
            return {"book_id": book_id, "formulas": 0}

        cache_col = None
        try:
            cache_col = await _get_collection(BUILD_CACHE_COLLECTION)
        except Exception:
            pass

        batch = max(1, settings.INDEX_BUILD_BATCH_SIZE)
        total = len(entries)
        for i in range(0, total, batch):
            await _enrich_batch(entries[i:i + batch], cache_col)
            done = min(i + batch, total)
            # This loop runs for minutes on a real book — without progress the
            # worker looks dead from the outside.
            logger.info(f"math_index: enriched {done}/{total} formulas for '{book_id}'")
            await jobs_col.update_one({"_id": job_id}, {"$set": {
                "progress": f"Enriching formulas {done}/{total}"}})

        await jobs_col.update_one({"_id": job_id}, {"$set": {
            "progress": f"Embedding {total} formulas"}})
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

        math_col = await _get_collection(MATH_COLLECTION)
        stored = 0
        for e, emb in zip(entries, embeddings):
            if not emb:
                continue
            doc = {
                "_id": e["_id"], "book_id": e["book_id"], "book_hash": e["book_hash"],
                "parent_chunk_id": e["parent_chunk_id"],
                "chapter_num": e["chapter_num"], "chapter_title": e["chapter_title"],
                "section_title": e["section_title"], "page": e["page"],
                "formula_latex": e["latex"], "formula_plain": e["formula_plain"],
                "context_sentence": e["context_sentence"],
                "concept_label": e["concept_label"], "variables": e["variables"],
                "embedding": emb, "created_at": datetime.now(timezone.utc),
            }
            try:
                await math_col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                stored += 1
            except Exception as exc:
                logger.warning(f"math_index upsert failed for {doc['_id']}: {exc}")

        try:
            await math_col.create_index("parent_chunk_id")
            await math_col.create_index("book_id")
        except Exception:
            pass

        await jobs_col.update_one({"_id": job_id}, {"$set": {
            "status": "done", "formulas": stored,
            "finished_at": datetime.now(timezone.utc)}})
        logger.info(f"math_index: built {stored} formula docs for book '{book_id}'")
        return {"book_id": book_id, "formulas": stored}

    except Exception as exc:
        await jobs_col.update_one({"_id": job_id}, {"$set": {
            "status": "failed", "error": str(exc)[:500],
            "finished_at": datetime.now(timezone.utc)}})
        raise


# ── Retrieval + prompt rendering ───────────────────────────────────────────────

async def retrieve_formulas(
    query_embedding: list[float],
    book_id: str | None = None,
    k: int = 5,
) -> list[dict]:
    if not settings.MATH_INDEX_ENABLED:
        return []
    return await vector_search(
        query_embedding, k=k, book_id=book_id,
        collection_name=MATH_COLLECTION, index_name=MATH_INDEX_NAME,
    )


def render_formulas_block(formulas: list[dict]) -> str:
    """Render retrieved formula docs as a prompt section. Empty string if none."""
    if not formulas:
        return ""
    lines = ["KEY FORMULAS (verbatim from the textbook — base computations on these exact forms):"]
    for f in formulas:
        label = f.get("concept_label") or "formula"
        latex = f.get("formula_latex") or f.get("formula_plain", "")
        ctx = (f.get("context_sentence") or "")[:160]
        line = f"- {label}: {latex}"
        if ctx:
            line += f"   ({ctx})"
        lines.append(line)
    return "\n".join(lines)


async def delete_math_index(book_id: str) -> int:
    try:
        col = await _get_collection(MATH_COLLECTION)
        result = await col.delete_many({"book_id": book_id})
        jobs_col = await _get_collection(INDEX_JOBS_COLLECTION)
        await jobs_col.delete_one({"_id": f"math:{book_id}"})
        return result.deleted_count
    except Exception as exc:
        logger.warning(f"delete_math_index failed (non-fatal): {exc}")
        return 0


async def math_index_status() -> list[dict]:
    """Per-book build status + formula counts."""
    out: list[dict] = []
    try:
        jobs_col = await _get_collection(INDEX_JOBS_COLLECTION)
        math_col = await _get_collection(MATH_COLLECTION)
        async for job in jobs_col.find({"index": "math"}):
            book_id = job.get("book_id")
            count = await math_col.count_documents({"book_id": book_id})
            out.append({
                "book_id": book_id, "status": job.get("status"),
                "formulas": count, "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"), "error": job.get("error"),
            })
    except Exception as exc:
        logger.warning(f"math_index_status failed: {exc}")
    return out
