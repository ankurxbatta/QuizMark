"""
chunking.py — hybrid text chunking for book ingestion.

Strategy (≈80% recursive / ≈20% semantic):
  • recursive_split — LangChain RecursiveCharacterTextSplitter with overlap.
    Primary splitter for all buffered text. Guarantees NO data loss:
      - text shorter than min_chars is returned as-is (never dropped)
      - a tail segment shorter than min_chars is merged into the previous
        chunk instead of being discarded (the old splitter dropped it)
  • semantic_split — embedding-based breakpoint splitting (same algorithm as
    LangChain's SemanticChunker): sentences are embedded, and the text is cut
    where consecutive-sentence cosine distance spikes above a percentile
    threshold. Applied to the densest ~20% of chunks (SEMANTIC_CHUNK_RATIO).
"""
from __future__ import annotations

import logging
import math
import re

from app.core.config import settings

logger = logging.getLogger(__name__)

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    _LANGCHAIN_SPLITTER_AVAILABLE = True
except ImportError:
    _LANGCHAIN_SPLITTER_AVAILABLE = False
    logger.warning("langchain-text-splitters not installed — using fallback splitter")

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


# ── Recursive splitting (primary, ~80%) ────────────────────────────────────────

def recursive_split(
    text: str,
    max_chars: int,
    min_chars: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """
    Split text into chunks of at most ~max_chars with overlap, losing nothing.
    A tail below min_chars is merged into the previous chunk rather than dropped;
    text that is entirely below min_chars is returned as a single chunk.
    """
    text = text.strip()
    if not text:
        return []
    if min_chars is None:
        min_chars = settings.PDF_MIN_CHUNK_CHARS
    if overlap is None:
        overlap = settings.CHUNK_OVERLAP
    if len(text) <= max_chars:
        return [text]

    if _LANGCHAIN_SPLITTER_AVAILABLE:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=max_chars,
            chunk_overlap=min(overlap, max_chars // 4),
            separators=["\n\n", "\n", ". ", " ", ""],
            keep_separator=True,
        )
        parts = [p.strip() for p in splitter.split_text(text) if p.strip()]
    else:
        parts = _fallback_split(text, max_chars)

    if not parts:
        return [text]

    # Merge an undersized tail into the previous chunk — never discard it
    merged: list[str] = []
    for part in parts:
        if merged and len(part) < min_chars:
            merged[-1] = f"{merged[-1]}\n{part}"
        else:
            merged.append(part)
    return merged


def _fallback_split(text: str, max_chars: int) -> list[str]:
    """Paragraph-then-hard split used only if langchain isn't installed."""
    parts: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 <= max_chars:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                parts.append(buf)
            while len(para) > max_chars:
                parts.append(para[:max_chars])
                para = para[max_chars:]
            buf = para
    if buf:
        parts.append(buf)
    return parts


# ── Semantic splitting (refinement, ~20%) ──────────────────────────────────────

def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return 1.0 - dot / (na * nb)


async def semantic_split(
    text: str,
    embed_batch_fn,
    min_chars: int | None = None,
    breakpoint_percentile: int = 80,
) -> list[str]:
    """
    Split text at semantic topic boundaries (SemanticChunker algorithm):
    embed each sentence, measure cosine distance between neighbours, and cut
    where the distance exceeds the given percentile. Falls back to the whole
    text on any failure — never loses content.
    """
    if min_chars is None:
        min_chars = settings.PDF_MIN_CHUNK_CHARS

    sentences = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
    if len(sentences) < 6:
        return [text]

    try:
        embeddings = await embed_batch_fn(sentences)
        if len(embeddings) != len(sentences) or any(not e for e in embeddings):
            return [text]
    except Exception as exc:
        logger.debug(f"semantic_split embedding failed, keeping chunk whole: {exc}")
        return [text]

    distances = [
        _cosine_distance(embeddings[i], embeddings[i + 1])
        for i in range(len(embeddings) - 1)
    ]
    ranked = sorted(distances)
    threshold = ranked[min(len(ranked) - 1, (len(ranked) * breakpoint_percentile) // 100)]

    groups: list[list[str]] = [[sentences[0]]]
    for i, dist in enumerate(distances):
        if dist > threshold:
            groups.append([])
        groups[-1].append(sentences[i + 1])

    # Assemble, merging any undersized group into its predecessor
    out: list[str] = []
    for g in groups:
        seg = " ".join(g).strip()
        if not seg:
            continue
        if out and len(seg) < min_chars:
            out[-1] = f"{out[-1]} {seg}"
        else:
            out.append(seg)
    return out or [text]
