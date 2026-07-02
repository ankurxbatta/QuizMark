"""
retrieval_eval.py — retrieval evaluation harness (Phase 4 of MULTI_RAG_DESIGN).

Scores routed retrieval against a per-book golden-query set: for each query,
run the same `routed_retrieve` path generation/marking use, decide which
returned documents are relevant (page overlap or substring match), and compute
Recall@k, MRR and nDCG@k — overall and per specialist index.

Golden set format (JSON):

    {
      "book_id": "<book id or hash used at ingestion>",
      "queries": [
        {
          "query": "how is the sample standard deviation calculated",
          "chapter_num": 4,                        // optional scope
          "expect": {
            "pages": [118, 119],                   // relevant page numbers, and/or
            "contains": ["standard deviation"]     // substrings a relevant doc carries
          }
        }
      ]
    }

The metric functions are pure (no I/O) so they are unit-testable; only
`evaluate_golden` touches the network (one embedding call per query, then
vector searches). Run via `python scripts/eval_retrieval.py --golden <file>`.
"""
from __future__ import annotations

import math

# Fields checked for `contains` matches, across all doc shapes.
_TEXT_FIELDS = (
    "text", "latex", "formula_plain", "concept_name", "context_sentence",
    "caption", "description", "kind", "table_summary", "table_markdown",
    "section_title", "chapter_title",
)


# ── Pure metrics (binary relevance) ───────────────────────────────────────────

def recall_at_k(ranked_relevance: list[bool], total_relevant: int, k: int) -> float:
    """Fraction of ALL relevant docs found in the top k (0.0 if none exist)."""
    if total_relevant <= 0:
        return 0.0
    return sum(ranked_relevance[:k]) / total_relevant


def hit_at_k(ranked_relevance: list[bool], k: int) -> float:
    """1.0 if any relevant doc appears in the top k."""
    return 1.0 if any(ranked_relevance[:k]) else 0.0


def mrr(ranked_relevance: list[bool]) -> float:
    """Reciprocal rank of the first relevant doc (0.0 if none)."""
    for rank, relevant in enumerate(ranked_relevance, 1):
        if relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked_relevance: list[bool], k: int) -> float:
    """Normalised discounted cumulative gain with binary gains."""
    gains = [1.0 if r else 0.0 for r in ranked_relevance[:k]]
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    n_relevant = min(sum(ranked_relevance), k)
    if n_relevant == 0:
        return 0.0
    ideal = sum(1.0 / math.log2(i + 2) for i in range(n_relevant))
    return dcg / ideal


# ── Relevance judgement ───────────────────────────────────────────────────────

def _doc_pages(doc: dict) -> set[int]:
    if doc.get("page") is not None:
        return {int(doc["page"])}
    start = doc.get("page_start")
    end = doc.get("page_end", start)
    if start is None:
        return set()
    return set(range(int(start), int(end) + 1))


def doc_matches(doc: dict, expect: dict) -> bool:
    """A doc is relevant if it overlaps an expected page OR carries an
    expected substring (case-insensitive) in any text-bearing field."""
    pages = expect.get("pages") or []
    if pages and _doc_pages(doc) & {int(p) for p in pages}:
        return True
    needles = [str(s).lower() for s in (expect.get("contains") or []) if str(s).strip()]
    if needles:
        haystack = " ".join(
            str(doc.get(f) or "") for f in _TEXT_FIELDS
        ).lower()
        if any(needle in haystack for needle in needles):
            return True
    return False


def judge(ranked_docs: list[dict], expect: dict) -> list[bool]:
    return [doc_matches(doc, expect) for doc in ranked_docs]


def score_query(fused_lists: dict[str, list[dict]], expect: dict, k: int) -> dict:
    """Metrics for one golden query. `fused_lists` maps specialist name →
    ranked docs (e.g. {"text": [...], "formula": [...], ...})."""
    out: dict[str, dict] = {}
    for name, docs in fused_lists.items():
        rel = judge(docs, expect)
        out[name] = {
            "hit@k": hit_at_k(rel, k),
            "mrr": mrr(rel),
            "ndcg@k": ndcg_at_k(rel, k),
            "returned": len(docs),
            "relevant_returned": sum(rel),
        }
    return out


def aggregate(per_query: list[dict]) -> dict:
    """Mean of each metric across queries, per specialist."""
    agg: dict[str, dict[str, float]] = {}
    counts: dict[str, int] = {}
    for query_scores in per_query:
        for name, metrics in query_scores.items():
            slot = agg.setdefault(name, {"hit@k": 0.0, "mrr": 0.0, "ndcg@k": 0.0})
            for metric in slot:
                slot[metric] += metrics[metric]
            counts[name] = counts.get(name, 0) + 1
    for name, slot in agg.items():
        for metric in slot:
            slot[metric] = round(slot[metric] / max(1, counts[name]), 4)
        slot["queries"] = counts[name]
    return agg


# ── End-to-end harness (network: 1 embedding call per query) ─────────────────

async def evaluate_golden(golden: dict, k: int = 8) -> dict:
    """Run every golden query through routed_retrieve and score it."""
    from app.services.llm_service import llm_service as llm
    from app.services.retrieval_router import routed_retrieve

    book_id = golden.get("book_id")
    queries = golden.get("queries") or []
    if not queries:
        raise ValueError("golden set has no queries")
    per_query: list[dict] = []
    details: list[dict] = []
    for entry in queries:
        query = entry["query"]
        embedding = await llm.embed(query)
        fused = await routed_retrieve(
            [query], [embedding],
            book_id=book_id,
            chapter_num=entry.get("chapter_num"),
            k=k,
        )
        fused_lists = {
            "text": fused.text_chunks,
            "formula": fused.formulas,
            "figure": fused.figures,
            "table": fused.tables,
            "fused": fused.text_chunks + fused.formulas + fused.figures + fused.tables,
        }
        scores = score_query(fused_lists, entry.get("expect") or {}, k)
        per_query.append(scores)
        details.append({"query": query, "scores": scores})

    return {
        "book_id": book_id,
        "k": k,
        "num_queries": len(queries),
        "summary": aggregate(per_query),
        "per_query": details,
    }
