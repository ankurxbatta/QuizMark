"""
ingestion_chain.py — LangChain (LCEL) pipeline for one window of book chunks.

    extract (ChunkAccumulator, upstream)
        → clean    : strip PDF noise from text/math/image/table fields
        → dedupe   : one canonical home per piece of content — prose in text,
                     tables in table_texts, novel math in math_text; inline
                     duplicates removed so nothing is stored or embedded twice
        → semantic : re-split the densest ~20% of chunks at topic boundaries
                     (SEMANTIC_CHUNK_RATIO; the other ~80% stay recursive)
        → math     : validation worker — structural checks on every chunk +
                     LLM repair of operators lost in PDF extraction (∑fm / ∑f)
        → vision   : chart description + math-rect LaTeX transcription,
                     both passes running concurrently
        → embed    : one batched embedding call per window

Each stage is a RunnableLambda over a shared context dict:
    {"chunks": [...], "pdf_bytes": b"...", "job_id": str, "embeddings": [...]}

If langchain-core isn't installed the same stages run as a plain sequential
pipeline, so ingestion never hard-depends on the package being present.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import math as _math
import re

from app.core.config import settings

logger = logging.getLogger(__name__)

try:
    from langchain_core.runnables import RunnableLambda
    _LANGCHAIN_AVAILABLE = True
except ImportError:
    _LANGCHAIN_AVAILABLE = False
    logger.warning("langchain-core not installed — ingestion chain runs in fallback mode")


def _is_redundant(part: str, base_tokens: set[str], threshold: float = 0.8) -> bool:
    """
    True if `part`'s word/number content is already present in the chunk text.
    Page text already contains table cells and raw math spans, so re-appending
    them to the embedding text counts the same content twice (or three times
    for an equation inside a table). Token overlap is used rather than substring
    matching because tables are reformatted as markdown rows.
    """
    tokens = re.findall(r"\w+", part.lower())
    if not tokens:
        return True
    hits = sum(1 for t in tokens if t in base_tokens)
    return hits / len(tokens) >= threshold


def chunk_embedding_text(chunk) -> str:
    """
    Text representation of a chunk used for its embedding. Table and math
    content that merely duplicates the main text is skipped — only novel
    content (vision LaTeX, OCR'd tables) is appended, so no equation or table
    is counted more than once. The stored chunk keeps all fields untouched.
    """
    base_tokens = set(re.findall(r"\w+", chunk.text.lower()))
    parts = [
        f"{chunk.chapter_title} {chunk.section_title}",
        chunk.text[:1500],
    ]
    for label, values in (
        ("Tables", getattr(chunk, "table_texts", [])),
        ("Images and charts", getattr(chunk, "image_texts", [])),
    ):
        novel = [v for v in values if not _is_redundant(v, base_tokens)]
        if novel:
            parts.append(f"{label}:\n" + "\n".join(novel)[:1200])
    math_text = getattr(chunk, "math_text", "")
    if math_text and not _is_redundant(math_text, base_tokens):
        parts.append(f"Formula snippets:\n{math_text[:800]}")
    return "\n\n".join(part for part in parts if part)


# ── Stage: clean ───────────────────────────────────────────────────────────────

async def _clean_stage(ctx: dict) -> dict:
    from app.services.text_cleaner import clean_chunk_doc

    for c in ctx["chunks"]:
        try:
            cleaned = clean_chunk_doc({
                "text": getattr(c, "text", ""),
                "math_text": getattr(c, "math_text", ""),
                "image_texts": getattr(c, "image_texts", []),
                "table_texts": getattr(c, "table_texts", []),
                "key_terms": getattr(c, "key_terms", []),
            })
            c.text = cleaned["text"]
            c.math_text = cleaned.get("math_text", "")
            c.image_texts = cleaned.get("image_texts", [])
            c.table_texts = cleaned.get("table_texts", [])
            c.key_terms = cleaned.get("key_terms", [])
        except Exception:
            pass  # never let the cleaner break ingest
    return ctx


# ── Stage: dedupe (single canonical home per content) ──────────────────────────

async def _dedupe_stage(ctx: dict) -> dict:
    from app.services.chunk_validator import dedupe_chunk_content

    for c in ctx["chunks"]:
        dedupe_chunk_content(c)
    return ctx


# ── Stage: semantic re-split (~20%) ────────────────────────────────────────────

async def _semantic_stage(ctx: dict) -> dict:
    from app.services.chunking import semantic_split
    from app.services.llm_service import llm_service

    chunks = ctx["chunks"]
    eligible = [
        c for c in chunks
        if len(getattr(c, "text", "")) >= settings.SEMANTIC_MIN_CHARS
    ]
    if not eligible:
        return ctx

    # The densest SEMANTIC_CHUNK_RATIO of the window gets semantic boundaries
    quota = max(1, _math.ceil(len(chunks) * settings.SEMANTIC_CHUNK_RATIO))
    eligible.sort(key=lambda c: getattr(c, "teaching_density", 0.0), reverse=True)
    selected = set(id(c) for c in eligible[:quota])

    out = []
    for c in chunks:
        if id(c) not in selected:
            out.append(c)
            continue
        try:
            parts = await semantic_split(c.text, llm_service.embed_batch)
        except Exception:
            parts = [c.text]
        if len(parts) <= 1:
            out.append(c)
            continue
        for i, part in enumerate(parts):
            sub = dataclasses.replace(c, text=part)
            sub.image_texts = list(c.image_texts)
            sub.table_texts = list(c.table_texts)
            sub.key_terms = list(c.key_terms)
            if i > 0:
                # Rects stay on the first sub-chunk only, so each figure or
                # formula region is sent to the vision model exactly once.
                sub.figure_rects = []
                sub.math_rects = []
            out.append(sub)
    ctx["chunks"] = out
    return ctx


# ── Stage: math (validation worker) ────────────────────────────────────────────

async def _math_stage(ctx: dict) -> dict:
    from app.services.chunk_validator import validate_chunks

    try:
        await validate_chunks(ctx["chunks"], job_id=ctx.get("job_id"))
    except Exception as exc:
        logger.warning(f"validation stage failed (non-fatal): {exc}")
    return ctx


# ── Stage: vision ──────────────────────────────────────────────────────────────

async def _vision_stage(ctx: dict) -> dict:
    if not settings.ENABLE_VISION_EXTRACTION:
        return ctx
    from app.services.pdf_extractor import describe_graph_chunks, transcribe_math_chunks

    chunks, pdf_bytes, job_id = ctx["chunks"], ctx["pdf_bytes"], ctx.get("job_id")
    concurrency = max(1, settings.INGEST_VISION_CONCURRENCY)
    jobs = []
    if any(getattr(c, "figure_rects", None) for c in chunks):
        jobs.append(describe_graph_chunks(chunks, pdf_bytes, concurrency=concurrency, job_id=job_id))
    if any(getattr(c, "math_rects", None) for c in chunks):
        jobs.append(transcribe_math_chunks(chunks, pdf_bytes, concurrency=concurrency, job_id=job_id))
    if jobs:
        # Both vision passes share the window — run them concurrently
        results = await asyncio.gather(*jobs, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"Vision pass on window failed (non-fatal): {r}")
    return ctx


# ── Stage: embed ───────────────────────────────────────────────────────────────

async def _embed_stage(ctx: dict) -> dict:
    from app.services.llm_service import llm_service

    chunks = ctx["chunks"]
    texts = [chunk_embedding_text(c) for c in chunks]
    batch_size = max(1, int(getattr(settings, "EMBEDDING_BATCH_SIZE", 100)))
    embeddings: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        try:
            batch_embs = await llm_service.embed_batch(batch)
        except Exception as exc:
            logger.warning(
                f"embed_batch failed for batch {start}-{start + len(batch)}; "
                f"per-chunk fallback: {exc}"
            )
            batch_embs = []
        if len(batch_embs) != len(batch):
            # Top up per-chunk so chunks/embeddings stay aligned — one failed
            # batch never kills the window, only its own chunks degrade.
            batch_embs = []
            for t in batch:
                try:
                    batch_embs.append(await llm_service.embed(t))
                except Exception:
                    batch_embs.append([])
        embeddings.extend(batch_embs)
    ctx["embeddings"] = embeddings
    return ctx


# ── Chain assembly ─────────────────────────────────────────────────────────────

_STAGES = (_clean_stage, _dedupe_stage, _semantic_stage, _math_stage, _vision_stage, _embed_stage)
_chain = None


def _build_chain():
    chain = RunnableLambda(_STAGES[0])
    for stage in _STAGES[1:]:
        chain = chain | RunnableLambda(stage)
    return chain


async def run_ingest_chain(ctx: dict) -> dict:
    """Run one window's chunks through clean → semantic → math → vision → embed."""
    global _chain
    if _LANGCHAIN_AVAILABLE:
        if _chain is None:
            _chain = _build_chain()
        return await _chain.ainvoke(ctx)
    for stage in _STAGES:
        ctx = await stage(ctx)
    return ctx
