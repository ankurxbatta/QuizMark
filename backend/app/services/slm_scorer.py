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

# ── HIGH-route full-credit gate ───────────────────────────────────────────────
# The no-LLM HIGH shortcut is only taken when an answer is UNAMBIGUOUSLY full
# credit: very high semantic alignment to the model answer AND strong coverage of
# the rubric's key terms. Anything less (a partial or merely-plausible answer)
# falls through to the LLM path so a real correctness check is applied. These are
# deliberately strict; override via settings if product tuning is needed.
_FULL_CREDIT_SEM = getattr(settings, "SLM_FULL_CREDIT_SEM", 0.92)
_FULL_CREDIT_KW = getattr(settings, "SLM_FULL_CREDIT_KW", 0.60)


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
    """Fraction of rubric keywords present in the answer.

    Matching is word/token based, NOT substring: we tokenise the answer with the
    same pattern used to extract keywords and test set membership. Substring
    matching (`kw in text`) over-counted spurious hits — e.g. the keyword "var"
    matching inside "variance", or "mean" inside "meaning" — which inflated
    coverage and pushed fluent-but-wrong answers onto the no-LLM fast path.
    """
    if not keywords:
        return 0.5
    tokens = set(re.findall(r"\b[a-z][a-z0-9\-]{2,}\b", student_answer.lower()))
    hits = sum(1 for kw in keywords if kw in tokens)
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

    # ── Route selection ───────────────────────────────────────────────────────
    # The HIGH (no-LLM) shortcut is restricted to answers that are clearly full
    # credit. Previously ANY confidence >= CONFIDENCE_HIGH accepted
    # `sem * max_marks` as the FINAL mark with no correctness check, which:
    #   • over-marked fluent-but-wrong answers (rubric terms reused → high
    #     confidence → a partial mark it never earned), and
    #   • under-marked correct paraphrases (capped at ~sem*max, e.g. 80%).
    # Now HIGH awards FULL marks only when semantic alignment AND keyword
    # coverage are both very high; every partial case is sent to the LLM (we
    # prefer the LLM when in doubt).
    clearly_full_credit = sem_score >= _FULL_CREDIT_SEM and kw_score >= _FULL_CREDIT_KW
    if clearly_full_credit:
        route = "HIGH"
        provisional_mark = float(max_marks)
    elif confidence >= settings.CONFIDENCE_MID:
        route = "MID"
        provisional_mark = round(sem_score * max_marks, 2)
    else:
        route = "LOW"
        provisional_mark = round(sem_score * max_marks, 2)

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
