"""
retrieval_router.py — the strategic connector between the specialist RAG
indexes (Phase 3 of MULTI_RAG_DESIGN).

Routes queries to the right specialist by intent, fuses the result lists with
Reciprocal-Rank Fusion, and expands hits across the parent_chunk_id cross-links
(a formula hit pulls in its source chunk; a strong chunk hit pulls in its
formulas/figures). Heuristic intent classification only — no LLM calls — so it
is cheap enough for the marking path.

Every specialist search degrades independently: an empty or failing index
simply contributes nothing, and the result is exactly the pre-Phase-3
text-only retrieval.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from app.core.config import settings
from app.services.reranker import rerank_results
from app.services.mongo_vector_store import (
    CHUNKS_COLLECTION,
    _get_collection,
    vector_search,
)

logger = logging.getLogger(__name__)

INTENT_CONCEPTUAL = "conceptual"
INTENT_COMPUTATIONAL = "computational"
INTENT_VISUAL = "visual"

_COMPUTATIONAL_RE = re.compile(
    r"formula|calculat|comput|solve|equation|how (do|to) .*(find|work out)"
    r"|standard deviation|variance|probability|confidence interval|test statistic"
    r"|z[- ]?score|t[- ]?score|p[- ]?value|[=√∑∫^]|sqrt|x_bar|mu\b|sigma\b",
    re.IGNORECASE,
)
_VISUAL_RE = re.compile(
    r"graph|chart|figure|histogram|scatter|boxplot|box plot|bar (chart|graph)"
    r"|pie chart|plot\b|table\b|distribution shape|skew|axis|axes|trend|outlier",
    re.IGNORECASE,
)


def classify_intent(query: str) -> str:
    """Heuristic intent for one query. Visual wins over computational on ties
    (visual signals are rarer and more specific)."""
    if _VISUAL_RE.search(query or ""):
        return INTENT_VISUAL
    if _COMPUTATIONAL_RE.search(query or ""):
        return INTENT_COMPUTATIONAL
    return INTENT_CONCEPTUAL


# ── Reciprocal-Rank Fusion ─────────────────────────────────────────────────────

def rrf_fuse(result_lists: list[list[dict]], k_const: int | None = None) -> list[dict]:
    """
    Fuse ranked result lists: score(doc) = Σ 1 / (k_const + rank).
    Documents are identified by _id; the first-seen copy is kept.
    """
    if k_const is None:
        k_const = settings.RRF_K
    scores: dict[str, float] = {}
    docs: dict[str, dict] = {}
    for results in result_lists:
        for rank, doc in enumerate(results, 1):
            doc_id = str(doc.get("_id", ""))
            if not doc_id:
                continue
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k_const + rank)
            docs.setdefault(doc_id, doc)
    ranked = sorted(scores, key=scores.get, reverse=True)
    return [docs[doc_id] for doc_id in ranked]


# ── Fused output ───────────────────────────────────────────────────────────────

@dataclass
class FusedContext:
    text_chunks: list[dict] = field(default_factory=list)
    formulas: list[dict] = field(default_factory=list)
    figures: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)

    def to_prompt(self, max_chunks: int = 8) -> str:
        from app.services.figure_index import render_figures_block
        from app.services.math_index import render_formulas_block
        from app.services.table_index import render_tables_block

        parts: list[str] = []
        if self.text_chunks:
            blocks = []
            for i, chunk in enumerate(self.text_chunks[:max_chunks], 1):
                section = f"{chunk.get('chapter_title', '')} — {chunk.get('section_title', '')}"
                blocks.append(f"[TEXTBOOK {i}: {section}]\n{chunk.get('text', '')}")
            parts.append("\n\n".join(blocks))
        for block in (
            render_formulas_block(self.formulas),
            render_figures_block(self.figures),
            render_tables_block(self.tables),
        ):
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    def specialist_block(self) -> str:
        """Render ONLY the specialist (formula / figure / table) index blocks.

        Used to inject the dedicated math/figure/table indexes into the mainline
        (Round 1) generation prompt alongside the chunk text, so the repaired
        LaTeX, real chart descriptions and table summaries are not left unused.
        Returns "" when no specialist content was retrieved.
        """
        from app.services.figure_index import render_figures_block
        from app.services.math_index import render_formulas_block
        from app.services.table_index import render_tables_block

        parts = [
            block
            for block in (
                render_formulas_block(self.formulas),
                render_tables_block(self.tables),
                render_figures_block(self.figures),
            )
            if block
        ]
        return "\n\n".join(parts)


# ── Cross-link expansion ───────────────────────────────────────────────────────

async def expand_to_parent_chunks(
    specialist_docs: list[dict],
    known_chunk_ids: set[str],
    limit: int | None = None,
) -> list[dict]:
    """Fetch parent chunks of specialist hits that aren't already in the result set."""
    if limit is None:
        limit = settings.EXPANSION_NEIGHBORS
    parents: list[dict] = []
    try:
        col = await _get_collection(CHUNKS_COLLECTION)
        for doc in specialist_docs:
            if len(parents) >= limit:
                break
            parent_id = doc.get("parent_chunk_id")
            if not parent_id or parent_id in known_chunk_ids:
                continue
            parent = await col.find_one({"_id": parent_id}, {"embedding": 0})
            if not parent:
                # Chunks ingested via the one-shot path keep a real ObjectId _id,
                # while specialist indexes store parent_chunk_id as a string. Retry
                # with the ObjectId form so cross-link expansion is not a silent
                # no-op for those books.
                try:
                    from bson import ObjectId

                    if ObjectId.is_valid(parent_id):
                        parent = await col.find_one(
                            {"_id": ObjectId(parent_id)}, {"embedding": 0}
                        )
                except Exception:
                    parent = None
            if parent:
                parent["_id"] = str(parent["_id"])
                known_chunk_ids.add(parent_id)
                parents.append(parent)
    except Exception as exc:
        logger.debug(f"cross-link expansion skipped: {exc}")
    return parents


# ── Routed retrieval ───────────────────────────────────────────────────────────

async def routed_retrieve(
    queries: list[str],
    embeddings: list[list[float]],
    book_id: str | None = None,
    chapter_num: int | None = None,
    k: int = 8,
) -> FusedContext:
    """
    Intent-routed multi-index retrieval with RRF fusion and cross-link expansion.

    `embeddings` must parallel `queries` (callers already embed their queries —
    no extra embedding calls are made here).
    """
    from app.services.figure_index import retrieve_figures
    from app.services.math_index import retrieve_formulas
    from app.services.table_index import retrieve_tables

    k_per_query = max(2, k // max(1, len(queries)))

    chunk_filters = {"chapter_num": chapter_num} if chapter_num is not None else None
    chunk_searches = [
        vector_search(emb, k=k_per_query, book_id=book_id, filters=chunk_filters)
        for emb in embeddings
    ]
    specialist_searches: list = []
    specialist_kinds: list[str] = []
    specialist_queries: list[str] = []
    for query, emb in zip(queries, embeddings):
        intent = classify_intent(query)
        if intent == INTENT_COMPUTATIONAL and settings.MATH_INDEX_ENABLED:
            specialist_searches.append(retrieve_formulas(emb, book_id=book_id, chapter_num=chapter_num, k=3))
            specialist_kinds.append("formula")
            specialist_queries.append(query)
        elif intent == INTENT_VISUAL:
            if settings.FIGURE_INDEX_ENABLED:
                specialist_searches.append(retrieve_figures(emb, book_id=book_id, chapter_num=chapter_num, k=3))
                specialist_kinds.append("figure")
                specialist_queries.append(query)
            if settings.TABLE_INDEX_ENABLED:
                specialist_searches.append(retrieve_tables(emb, book_id=book_id, chapter_num=chapter_num, k=2))
                specialist_kinds.append("table")
                specialist_queries.append(query)

    all_results = await asyncio.gather(
        *chunk_searches, *specialist_searches, return_exceptions=True
    )
    # Phase 4: rerank each result list against its originating sub-query
    # before fusion (no-op when RERANK_ENABLED is off).
    chunk_lists = [
        rerank_results("text", query, r)
        for query, r in zip(queries, all_results[:len(chunk_searches)])
        if isinstance(r, list)
    ]
    specialist_results = all_results[len(chunk_searches):]

    formulas_lists: list[list[dict]] = []
    figures_lists: list[list[dict]] = []
    tables_lists: list[list[dict]] = []
    for kind, query, result in zip(specialist_kinds, specialist_queries, specialist_results):
        if not isinstance(result, list):
            continue
        reranked = rerank_results(kind, query, result)
        {"formula": formulas_lists, "figure": figures_lists, "table": tables_lists}[kind].append(reranked)

    fused = FusedContext(
        text_chunks=rrf_fuse(chunk_lists)[:k],
        formulas=rrf_fuse(formulas_lists)[:5],
        figures=rrf_fuse(figures_lists)[:3],
        tables=rrf_fuse(tables_lists)[:2],
    )

    # Cross-link expansion: strong specialist hits pull in their source chunks.
    known_ids = {str(c.get("_id", "")) for c in fused.text_chunks}
    parents = await expand_to_parent_chunks(
        fused.formulas + fused.figures + fused.tables, known_ids
    )
    fused.text_chunks.extend(parents)
    return fused
