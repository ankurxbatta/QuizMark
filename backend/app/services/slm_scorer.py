"""
slm_scorer.py  —  Tier-1 SLM pre-scorer.

Runs three lightweight checks on a student answer before deciding
whether to invoke the full LLM:

  1. Keyword coverage   — what fraction of rubric keywords appear?
  2. Semantic similarity — cosine distance between answer embedding
                           and model-answer embedding (already stored in DB).
  3. SLM quick score    — phi3:mini asked for a 0-10 integer score only
                          (no explanation), super-fast.

These three signals are blended into a single confidence float [0, 1].
The confidence router in rag_pipeline.py uses it to pick the path.

Confidence thresholds (configurable in config.py):
  >= CONFIDENCE_HIGH  (default 0.85)  → Accept SLM mark, skip LLM entirely
  >= CONFIDENCE_MID   (default 0.55)  → RAG + offline LLM
  <  CONFIDENCE_MID                   → RAG wide + online LLM (or flag)
"""
import math
import re
from dataclasses import dataclass
from typing import Optional

from app.services.llm_service import slm_service
from app.core.config import settings


@dataclass
class SLMResult:
    """All signals produced by the Tier-1 SLM pre-scorer."""
    keyword_coverage: float      # 0.0 – 1.0
    semantic_similarity: float   # 0.0 – 1.0  (cosine)
    slm_raw_score: float         # 0.0 – 1.0  (normalised from 0-10)
    confidence: float            # blended signal, 0.0 – 1.0
    provisional_mark: float      # SLM-estimated mark (for HIGH path)
    route: str                   # "HIGH" | "MID" | "LOW"


def _extract_keywords(rubric: str) -> list[str]:
    """
    Pull meaningful words from the rubric.
    Strips stop words and short tokens.
    """
    stop = {
        "a","an","the","is","are","was","were","be","been","being",
        "have","has","had","do","does","did","will","would","could",
        "should","may","might","shall","must","can","need","dare",
        "and","or","but","if","in","on","at","to","for","of","with",
        "by","from","up","about","into","through","during","before",
        "after","above","below","between","each","every","both",
        "few","more","most","other","some","such","than","too","very",
        "just","because","as","until","while","although","though",
        "that","this","these","those","it","its","they","them","their",
        "1","2","3","4","5","mark","marks","marks:","correct","correct:",
    }
    tokens = re.findall(r"\b[a-z][a-z0-9\-]{2,}\b", rubric.lower())
    return [t for t in tokens if t not in stop]


def _keyword_coverage(student_answer: str, keywords: list[str]) -> float:
    if not keywords:
        return 0.5
    text = student_answer.lower()
    hits = sum(1 for kw in keywords if kw in text)
    return hits / len(keywords)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


async def slm_pre_score(
    question_text: str,
    model_answer: str,
    rubric: str,
    max_marks: float,
    student_answer: str,
    model_answer_embedding: Optional[list[float]] = None,
) -> SLMResult:
    """
    Tier-1 pre-scorer using keyword coverage + semantic similarity.

    The local SLM model call has been removed — all generation now goes
    through Gemini (more accurate, no local compute required).

    Weights: keyword_coverage 40%, semantic_similarity 60%.
    A HIGH confidence skips the LLM call entirely; MID/LOW route to Gemini.
    """
    # 1. Keyword coverage (fast, no model call)
    keywords = _extract_keywords(rubric)
    kw_score = _keyword_coverage(student_answer, keywords)

    # 2. Semantic similarity via Gemini embeddings
    if model_answer_embedding:
        answer_emb = await slm_service.embed(student_answer)
        sem_score = max(0.0, _cosine_similarity(answer_emb, model_answer_embedding))
    else:
        answer_emb, model_emb = await _embed_both(student_answer, model_answer)
        sem_score = max(0.0, _cosine_similarity(answer_emb, model_emb))

    # Blend: keyword 40%, semantic 60%
    confidence = round(0.40 * kw_score + 0.60 * sem_score, 4)

    # Provisional mark from semantic similarity
    provisional_mark = round(sem_score * max_marks, 2)

    if confidence >= settings.CONFIDENCE_HIGH:
        route = "HIGH"
    elif confidence >= settings.CONFIDENCE_MID:
        route = "MID"
    else:
        route = "LOW"

    return SLMResult(
        keyword_coverage=round(kw_score, 4),
        semantic_similarity=round(sem_score, 4),
        slm_raw_score=round(sem_score, 4),  # reuse sem_score for compatibility
        confidence=confidence,
        provisional_mark=provisional_mark,
        route=route,
    )


async def _embed_both(text_a: str, text_b: str):
    """Embed two texts concurrently."""
    import asyncio
    return await asyncio.gather(
        slm_service.embed(text_a),
        slm_service.embed(text_b),
    )
