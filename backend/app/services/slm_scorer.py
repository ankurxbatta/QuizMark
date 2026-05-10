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


# ── Prompt for fast SLM integer scoring ────────────────────────────────────
SLM_SCORE_PROMPT = """You are marking a student answer. Reply with a SINGLE integer 0-10 only.

Question: {question_text}
Model answer: {model_answer}
Rubric: {rubric}
Max marks: {max_marks}
Student answer: {student_answer}

Score 0-10 (proportional to marks earned). Reply with the integer ONLY."""


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


async def _slm_quick_score(
    question_text: str,
    model_answer: str,
    rubric: str,
    max_marks: float,
    student_answer: str,
) -> float:
    """
    Ask the SLM for a 0-10 integer score.
    Returns a normalised float 0.0-1.0.
    Falls back to 0.5 on any parse error.
    """
    prompt = SLM_SCORE_PROMPT.format(
        question_text=question_text,
        model_answer=model_answer[:400],
        rubric=rubric[:300],
        max_marks=max_marks,
        student_answer=student_answer[:600],
    )
    try:
        raw = await slm_service.generate(prompt)
        # Extract first integer in response
        match = re.search(r"\b([0-9]|10)\b", raw.strip())
        if match:
            score_int = int(match.group(1))
            return min(score_int / 10.0, 1.0)
    except Exception:
        pass
    return 0.5


async def slm_pre_score(
    question_text: str,
    model_answer: str,
    rubric: str,
    max_marks: float,
    student_answer: str,
    model_answer_embedding: Optional[list[float]] = None,
) -> SLMResult:
    """
    Run all three Tier-1 scoring signals and return an SLMResult.

    Weights:
      - keyword_coverage   30%
      - semantic_similarity 40%   (uses stored embedding if available)
      - slm_raw_score      30%
    """
    # 1. Keyword coverage (fast, no model call)
    keywords = _extract_keywords(rubric)
    kw_score = _keyword_coverage(student_answer, keywords)

    # 2. Semantic similarity (embed student answer, compare to stored model embedding)
    if model_answer_embedding:
        answer_emb = await slm_service.embed(student_answer)
        sem_score = max(0.0, _cosine_similarity(answer_emb, model_answer_embedding))
    else:
        # Embed both on the fly if stored embedding is missing
        answer_emb, model_emb = await _embed_both(student_answer, model_answer)
        sem_score = max(0.0, _cosine_similarity(answer_emb, model_emb))

    # 3. SLM quick integer score
    slm_score = await _slm_quick_score(
        question_text, model_answer, rubric, max_marks, student_answer
    )

    # Blend
    confidence = round(0.30 * kw_score + 0.40 * sem_score + 0.30 * slm_score, 4)

    # Provisional mark from SLM score
    provisional_mark = round(slm_score * max_marks, 2)

    # Route decision
    if confidence >= settings.CONFIDENCE_HIGH:
        route = "HIGH"
    elif confidence >= settings.CONFIDENCE_MID:
        route = "MID"
    else:
        route = "LOW"

    return SLMResult(
        keyword_coverage=round(kw_score, 4),
        semantic_similarity=round(sem_score, 4),
        slm_raw_score=round(slm_score, 4),
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
