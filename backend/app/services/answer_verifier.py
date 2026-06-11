"""
answer_verifier.py  —  numeric sanity check for generated model answers.

Generated model answers sometimes contain arithmetic errors (the value is
computed inline while writing prose). Because the marking pipeline treats the
model answer as ground truth, a wrong number there silently penalises correct
student answers.

LLMs are unreliable at arithmetic but reliable at *translation*, so the check
is split:
  1. LLM extracts the final calculation as a pure-Python math expression plus
     the value stated in the model answer.
  2. Python evaluates the expression deterministically (restricted namespace).
  3. If the stated value disagrees beyond tolerance, an LLM rewrite pass swaps
     in the computed value — the number itself never comes from the LLM.

Non-fatal by design: any failure leaves the original question untouched.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re

from app.services.llm_service import llm_service

logger = logging.getLogger(__name__)

RELATIVE_TOLERANCE = 0.02

# A model answer is worth recomputing when it both contains digits and smells
# like a calculation rather than a definition with incidental numbers.
_NUMERIC_SIGNALS = re.compile(
    r"(calculate|comput|probability|P\s*\(|=\s*[\d.]|approximately|≈|standard deviation"
    r"|mean of|expected value|z-score|formula yields)",
    re.IGNORECASE,
)

_EXTRACT_PROMPT = """You are checking the numeric correctness of a model answer for a statistics exam question.

Question:
{question_text}

Model Answer:
{model_answer}

Extract the FINAL numeric result the model answer states, and the calculation that produces it, as a pure Python arithmetic expression. You may use: + - * / ** ( ) and the functions comb(n, k), factorial(n), exp(x), sqrt(x), log(x), and the constants pi, e.

Examples:
  "P(X = 12) = C(20,12) * 0.35^12 * 0.65^8 ≈ 0.0004"
    → expression: "comb(20, 12) * 0.35**12 * 0.65**8", stated_value: 0.0004
  "P(X < 5) = e^-6 * (1 + 6 + 18 + 36 + 54) ≈ 0.265"
    → expression: "exp(-6) * (1 + 6 + 18 + 36 + 54)", stated_value: 0.265

Respond ONLY as valid JSON:
{{"has_computation": <bool>, "expression": <string or null>, "stated_value": <number or null>}}

Set has_computation to false if the answer states no computed numeric result
(e.g. it only quotes a formula or a given parameter like "p = 0.35").
"""

_REWRITE_PROMPT = """The following model answer for an exam question states an incorrect numeric result.

Question:
{question_text}

Model Answer:
{model_answer}

The correctly computed final value is: {correct_value}
The incorrect value currently stated is: {stated_value}

Rewrite the model answer keeping the original wording, formula and length as much as possible, but with the final result corrected to {correct_value} (round sensibly, 3-4 significant figures). Fix any intermediate values that are inconsistent with it. Respond ONLY with the rewritten model answer text, no preamble.
"""

# Charset whitelist for the extracted expression — digits, operators, parens,
# whitespace, and the allowed function/constant names only.
_EXPR_ALLOWED = re.compile(
    r"^[\d\s+\-*/().,]*"
    r"(?:[\d\s+\-*/().,]|comb|factorial|exp|sqrt|log|pi|e)*$"
)

_EVAL_NAMESPACE = {
    "comb": math.comb,
    "factorial": math.factorial,
    "exp": math.exp,
    "sqrt": math.sqrt,
    "log": math.log,
    "pi": math.pi,
    "e": math.e,
}


def _looks_numeric(question: dict) -> bool:
    # Objective questions carry a structured correct_answer key; rewriting
    # their model answer prose could contradict it. Marking for them is
    # deterministic anyway, so only short-answer questions are verified.
    if question.get("question_type") not in (None, "short_answer"):
        return False
    answer = question.get("model_answer", "") or ""
    if not re.search(r"\d", answer):
        return False
    blob = f"{question.get('question_text', '')} {answer}"
    return bool(_NUMERIC_SIGNALS.search(blob))


def _parse_json(raw: str) -> dict | None:
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def evaluate_expression(expression: str) -> float | None:
    """Deterministically evaluate an extracted arithmetic expression.

    Restricted namespace (math helpers only, no builtins) plus a charset
    whitelist and size caps so a malformed extraction can't run arbitrary
    code or hang the worker on absurd exponents.
    """
    expression = (expression or "").strip()
    if not expression or len(expression) > 300:
        return None
    if not _EXPR_ALLOWED.match(expression):
        return None
    # Cap exponent magnitude — comb()/factorial() are fine, but 9**9**9 isn't.
    for exp_part in re.findall(r"\*\*\s*\(?\s*-?\s*([\d.]+)", expression):
        try:
            if float(exp_part) > 200:
                return None
        except ValueError:
            return None
    if re.search(r"factorial\s*\(\s*([\d.]+)\s*\)", expression):
        for n in re.findall(r"factorial\s*\(\s*([\d.]+)\s*\)", expression):
            if float(n) > 500:
                return None
    try:
        value = eval(expression, {"__builtins__": {}}, dict(_EVAL_NAMESPACE))  # noqa: S307
        return float(value)
    except Exception:
        return None


async def _verify_one(question: dict) -> None:
    raw = await llm_service.generate(_EXTRACT_PROMPT.format(
        question_text=question.get("question_text", ""),
        model_answer=question.get("model_answer", ""),
    ))
    verdict = _parse_json(raw)
    if not verdict or not verdict.get("has_computation"):
        return

    stated = verdict.get("stated_value")
    computed = evaluate_expression(verdict.get("expression") or "")
    if computed is None or stated is None:
        return
    try:
        stated = float(stated)
    except (TypeError, ValueError):
        return

    denom = max(abs(computed), 1e-12)
    if abs(computed - stated) / denom <= RELATIVE_TOLERANCE:
        return  # model answer is numerically fine

    correct_value = float(f"{computed:.4g}")
    rewritten = (await llm_service.generate(_REWRITE_PROMPT.format(
        question_text=question.get("question_text", ""),
        model_answer=question.get("model_answer", ""),
        correct_value=correct_value,
        stated_value=stated,
    ))).strip()
    if not rewritten:
        return
    # The rewrite must actually contain the computed value; otherwise keep
    # the original rather than trust an off-script rewrite.
    if not re.search(re.escape(f"{correct_value:g}".rstrip("0").rstrip(".")), rewritten):
        logger.warning("[VERIFY] rewrite did not contain the computed value, keeping original")
        return
    logger.info(
        "[VERIFY] corrected %r: stated %s → computed %s",
        question.get("question_text", "")[:80], stated, correct_value,
    )
    question["model_answer"] = rewritten


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
