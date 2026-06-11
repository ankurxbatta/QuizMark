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


# ── MCQ option validation ──────────────────────────────────────────────────────
# Generated distractors are sometimes true statements (often a rephrasing of
# the correct option). With deterministic letter-comparison marking, a student
# who picks the synonymous distractor is wrongly given 0 — so every distractor
# must be verifiably false before the question is stored.

_MCQ_JUDGE_PROMPT = """You are reviewing a multiple-choice exam question for ambiguity.

Question:
{stem}

Options:
{options_block}

For EACH option independently, decide whether it is a factually correct answer to the question — not whether it is the "best" or intended answer. An option that restates another correct option in different words is also correct.

Respond ONLY as valid JSON:
{{"A": <bool>, "B": <bool>, "C": <bool>, "D": <bool>}}
"""

_MCQ_FIX_PROMPT = """This multiple-choice exam question has a problem: some of its wrong options are actually true statements, so more than one option is correct.

Question:
{stem}

Options:
{options_block}

The intended correct answer is {correct_letter}: {correct_text}

Rewrite ONLY the options {fix_letters} so each becomes a plausible but unambiguously FALSE answer to the question — a common misconception or error, NOT a rephrasing of the correct answer and NOT a true statement. Keep them similar in length and tone to the other options.

Respond ONLY as valid JSON mapping the rewritten letters to their new text, e.g.:
{{"D": "<new option text>"}}
"""


_MCQ_DISTRACTOR_PROMPT = """You are writing wrong options for a multiple-choice statistics exam question.

Question:
{stem}

Correct answer ({correct_letter}): {correct_text}

Write a plausible but unambiguously FALSE option for each of the letters {fill_letters}. Each one must:
- Be a believable answer a student with a common misconception might pick
- Be specific to this question's topic (no generic filler)
- Be factually false — never a rephrasing or special case of the correct answer
- Match the correct option's length and tone

Respond ONLY as valid JSON mapping each letter to its option text, e.g.:
{{"B": "<option text>", "C": "<option text>"}}
"""


def _options_block(options: dict[str, str]) -> str:
    return "\n".join(f"{letter}. {options[letter]}" for letter in sorted(options))


async def _fill_generic_distractors(question: dict, stem: str, options: dict[str, str]) -> None:
    """Replace boilerplate fallback distractors with topic-specific false ones."""
    letters = question.pop("_generic_distractors", None) or []
    correct = (question.get("correct_answer") or "").strip().upper()
    letters = [l for l in letters if l in options and l != correct]
    if not letters or correct not in options:
        return
    raw = await llm_service.generate(_MCQ_DISTRACTOR_PROMPT.format(
        stem=stem,
        correct_letter=correct,
        correct_text=options[correct],
        fill_letters=", ".join(letters),
    ))
    fixes = _parse_json(raw) or {}
    applied = []
    for letter in letters:
        new_text = str(fixes.get(letter) or fixes.get(letter.lower()) or "").strip()
        if new_text and new_text.lower() != options[correct].lower():
            options[letter] = new_text
            applied.append(letter)
    if applied:
        logger.info("[VERIFY-MCQ] replaced generic distractor(s) %s for %r", applied, stem[:80])


async def _verify_mcq_one(question: dict) -> None:
    from app.services.question_generator import _split_mcq_text

    stem, options = _split_mcq_text(question.get("question_text", ""))
    correct = (question.get("correct_answer") or "").strip().upper()
    if not stem or correct not in options or len(options) < 3:
        question.pop("_generic_distractors", None)
        return

    before = dict(options)

    # Pass 1: replace boilerplate fallback distractors with topic-specific
    # ones, then let the ambiguity judge validate the replacements too.
    await _fill_generic_distractors(question, stem, options)

    # Pass 2: judge each option independently for factual correctness.
    raw = await llm_service.generate(_MCQ_JUDGE_PROMPT.format(
        stem=stem, options_block=_options_block(options),
    ))
    verdict = _parse_json(raw)
    judged_true = (
        {k.upper() for k, v in verdict.items() if v is True and k.upper() in options}
        if verdict else {correct}
    )

    if correct not in judged_true:
        # The stored key itself failed the check — a single LLM judgement isn't
        # enough evidence to overturn the key, so just surface it in the logs.
        logger.warning(
            "[VERIFY-MCQ] stored key %s judged not-correct for %r — leaving unchanged",
            correct, stem[:80],
        )
        judged_true = {correct}

    extra_true = sorted(judged_true - {correct})
    if extra_true:
        raw_fix = await llm_service.generate(_MCQ_FIX_PROMPT.format(
            stem=stem,
            options_block=_options_block(options),
            correct_letter=correct,
            correct_text=options[correct],
            fix_letters=", ".join(extra_true),
        ))
        fixes = _parse_json(raw_fix) or {}
        applied = []
        for letter in extra_true:
            new_text = str(fixes.get(letter) or fixes.get(letter.lower()) or "").strip()
            if new_text and new_text.lower() != options[correct].lower():
                options[letter] = new_text
                applied.append(letter)
        if applied:
            logger.info(
                "[VERIFY-MCQ] rewrote ambiguous distractor(s) %s for %r",
                applied, stem[:80],
            )

    if options != before:
        question["question_text"] = "\n".join(
            [stem, *(f"{letter}. {options[letter]}" for letter in sorted(options))]
        )


async def verify_mcq_options(questions: list[dict]) -> list[dict]:
    """Ensure each MCQ has exactly one correct option; rewrites true distractors in place."""
    mcqs = [q for q in questions if q.get("question_type") == "mcq"]
    if not mcqs:
        return questions
    logger.info(f"[VERIFY-MCQ] checking {len(mcqs)} MCQ option sets")
    semaphore = asyncio.Semaphore(3)

    async def _bounded(q: dict) -> None:
        async with semaphore:
            try:
                await _verify_mcq_one(q)
            except Exception as exc:
                logger.warning(f"[VERIFY-MCQ] check failed (non-fatal): {exc}")

    await asyncio.gather(*[_bounded(q) for q in mcqs])
    return questions


async def verify_generated_questions(questions: list[dict]) -> list[dict]:
    """All post-generation quality passes: numeric recomputation + MCQ ambiguity."""
    await verify_numeric_model_answers(questions)
    await verify_mcq_options(questions)
    return questions
