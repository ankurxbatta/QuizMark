"""
answer_verifier.py  —  numeric sanity check for generated model answers.

Generated model answers sometimes contain arithmetic errors (the value is
computed inline while writing prose). Because the marking pipeline treats the
model answer as ground truth, a wrong number there silently penalises correct
student answers. This module re-derives the numeric result in a dedicated
step-by-step pass — far more reliable than inline generation — and rewrites
the model answer when the recomputation disagrees.

Non-fatal by design: any failure leaves the original question untouched.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from app.services.llm_service import llm_service

logger = logging.getLogger(__name__)

# A model answer is worth recomputing when it both contains digits and smells
# like a calculation rather than a definition with incidental numbers.
_NUMERIC_SIGNALS = re.compile(
    r"(calculate|comput|probability|P\s*\(|=\s*[\d.]|approximately|≈|standard deviation"
    r"|mean of|expected value|z-score|formula yields)",
    re.IGNORECASE,
)

_VERIFY_PROMPT = """You are checking the numeric correctness of a model answer for an exam question.

Question:
{question_text}

Model Answer:
{model_answer}

Recompute every numeric result in the model answer yourself, step by step, showing intermediate values. Then compare with the stated result(s). Small rounding differences (within 2% relative error) count as correct.

Respond ONLY as valid JSON:
{{"correct": <bool>, "corrected_model_answer": <string or null>, "working": "<one-line summary of your recomputation>"}}

If correct is true, corrected_model_answer must be null.
If correct is false, corrected_model_answer must be the full model answer rewritten with the right value(s), keeping the original wording and length as much as possible.
"""


def _looks_numeric(question: dict) -> bool:
    answer = question.get("model_answer", "") or ""
    if not re.search(r"\d", answer):
        return False
    blob = f"{question.get('question_text', '')} {answer}"
    return bool(_NUMERIC_SIGNALS.search(blob))


def _parse_verdict(raw: str) -> dict | None:
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        verdict = json.loads(match.group(0))
        if not isinstance(verdict, dict) or "correct" not in verdict:
            return None
        return verdict
    except Exception:
        return None


async def _verify_one(question: dict) -> None:
    prompt = _VERIFY_PROMPT.format(
        question_text=question.get("question_text", ""),
        model_answer=question.get("model_answer", ""),
    )
    raw = await llm_service.generate(prompt)
    verdict = _parse_verdict(raw)
    if verdict is None:
        logger.warning("[VERIFY] unparseable verdict, keeping original model answer")
        return
    if verdict.get("correct"):
        return
    corrected = (verdict.get("corrected_model_answer") or "").strip()
    if not corrected:
        return
    logger.info(
        "[VERIFY] corrected numeric model answer for %r: %s",
        question.get("question_text", "")[:80],
        verdict.get("working", ""),
    )
    question["model_answer"] = corrected


async def verify_numeric_model_answers(questions: list[dict]) -> list[dict]:
    """Recompute numeric model answers in place; returns the same list."""
    numeric = [q for q in questions if _looks_numeric(q)]
    if not numeric:
        return questions
    logger.info(f"[VERIFY] checking {len(numeric)}/{len(questions)} numeric model answers")
    semaphore = asyncio.Semaphore(3)

    async def _bounded(q: dict) -> None:
        async with semaphore:
            try:
                await _verify_one(q)
            except Exception as exc:
                logger.warning(f"[VERIFY] verification failed (non-fatal): {exc}")

    await asyncio.gather(*[_bounded(q) for q in numeric])
    return questions
