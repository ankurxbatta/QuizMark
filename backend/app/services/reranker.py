"""
reranker.py — per-specialist result reranking (Phase 4 of MULTI_RAG_DESIGN).

Purely lexical and deterministic: no LLM calls, no new dependencies, so it is
cheap enough to sit on both the generation and marking retrieval paths. Each
specialist result list is reranked against its ORIGINATING sub-query before
RRF fusion, blending the vector rank with modality-appropriate lexical signals:

  text    — query-term overlap with the chunk text and section titles
  formula — shared math vocabulary (greek letters, operators, stat terms)
            against the LaTeX + plain-English rendering
  figure  — figure-kind keywords in the query matching the classified kind,
            plus caption/description term overlap
  table   — header and summary term overlap

Disabled (RERANK_ENABLED=false) or degenerate inputs return the list unchanged,
preserving the Phase 3 behaviour exactly.
"""
from __future__ import annotations

import re

from app.core.config import settings

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

# Function words carry no retrieval signal and dilute the overlap fraction.
_STOPWORDS = {
    "the", "and", "for", "with", "what", "how", "does", "this", "that", "from",
    "are", "was", "were", "which", "into", "about", "when", "then", "than",
    "them", "its", "can", "will", "each", "between", "using", "use", "you",
    "your", "have", "has", "had", "would", "could", "should", "there", "their",
}

# Query words that signal a specific figure kind (mirrors FIGURE_KINDS).
_KIND_SYNONYMS = {
    "histogram": "histogram",
    "bar": "bar",
    "scatter": "scatter",
    "scatterplot": "scatter",
    "boxplot": "boxplot",
    "box": "boxplot",
    "line": "line",
    "pie": "pie",
    "diagram": "diagram",
}

# Math vocabulary: greek names, operators spelled out, and core stat terms.
# Single-character variables are too noisy to match lexically; these are the
# tokens that reliably identify WHICH formula a query is about.
_MATH_VOCAB = {
    "sigma", "mu", "alpha", "beta", "lambda", "theta", "chi", "rho", "nu",
    "sqrt", "sum", "integral", "mean", "median", "mode", "variance",
    "deviation", "probability", "proportion", "correlation", "regression",
    "interval", "score", "statistic", "frequency", "midpoint", "quartile",
    "percentile", "factorial", "combination", "permutation", "expected",
}

# Which document fields carry matchable text, per specialist kind.
_FIELDS = {
    "text": ("text", "section_title", "chapter_title"),
    "formula": ("latex", "formula_plain", "concept_name", "context_sentence"),
    "figure": ("kind", "caption", "description"),
    "table": ("table_summary", "table_markdown", "headers"),
}


def _tokens(value) -> set[str]:
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    raw = _TOKEN_RE.findall(str(value or "").lower())
    # Naive plural folding so "midpoints" matches "midpoint" etc.
    return {
        t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t
        for t in raw
        if t not in _STOPWORDS
    }


def _doc_tokens(doc: dict, kind: str) -> set[str]:
    tokens: set[str] = set()
    for field_name in _FIELDS.get(kind, _FIELDS["text"]):
        tokens |= _tokens(doc.get(field_name))
    return tokens


def _lexical_score(kind: str, query_tokens: set[str], doc: dict) -> float:
    if not query_tokens:
        return 0.0
    doc_tokens = _doc_tokens(doc, kind)
    score = len(query_tokens & doc_tokens) / len(query_tokens)

    if kind == "formula":
        shared_vocab = query_tokens & doc_tokens & _MATH_VOCAB
        score += 0.25 * min(len(shared_vocab), 2)
    elif kind == "figure":
        wanted_kinds = {_KIND_SYNONYMS[t] for t in query_tokens if t in _KIND_SYNONYMS}
        if wanted_kinds and str(doc.get("kind", "")).lower() in wanted_kinds:
            score += 0.5
    return score


def rerank_results(kind: str, query: str, docs: list[dict]) -> list[dict]:
    """Rerank one specialist result list against its originating query.

    Blends the original vector rank (normalised linearly, so adjacent ranks
    differ by alpha/len) with the lexical score:
        blended = alpha * (len-rank)/len + (1-alpha) * lexical
    The sort is stable, so ties keep vector order and an all-zero lexical
    pass is a no-op.
    """
    if not settings.RERANK_ENABLED or len(docs) < 2:
        return docs
    alpha = settings.RERANK_ALPHA
    query_tokens = _tokens(query)
    n = len(docs)
    blended = [
        alpha * (n - rank) / n + (1 - alpha) * _lexical_score(kind, query_tokens, doc)
        for rank, doc in enumerate(docs)
    ]
    order = sorted(range(len(docs)), key=lambda i: blended[i], reverse=True)
    return [docs[i] for i in order]
