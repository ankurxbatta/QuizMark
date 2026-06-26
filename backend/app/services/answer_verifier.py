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

from app.core.config import settings
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
        from app.services.question_generator import unmangle_latex
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        return unmangle_latex(data) if isinstance(data, dict) else None
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


# ── Numeric correctness for objective (mcq / true_false) questions ──────────────
# The stored correct_answer for an objective question is treated as ground truth
# by the deterministic marking fast path. When the question involves a
# computation (e.g. a Poisson/binomial probability), the LLM that generated it
# may have picked / asserted the wrong value. We reuse the extract+evaluate
# mechanism — the number is always produced by Python, never by the LLM — to find
# the correct option (MCQ) or the correct boolean (true_false).

_MCQ_EXTRACT_PROMPT = """You are checking the numeric correctness of a multiple-choice statistics exam question.

Question stem:
{stem}

Options:
{options_block}

If answering this question requires computing a single numeric value, extract the calculation as a pure Python arithmetic expression. You may use: + - * / ** ( ) and the functions comb(n, k), factorial(n), exp(x), sqrt(x), log(x), and the constants pi, e.

Examples:
  Poisson P(X=3), lambda=10  → "10**3 * exp(-10) / factorial(3)"
  Geometric first success on 5th trial, p=0.01  → "(1 - 0.01)**4 * 0.01"
  Binomial P(X=12), n=20, p=0.35  → "comb(20, 12) * 0.35**12 * 0.65**8"

Respond ONLY as valid JSON:
{{"has_computation": <bool>, "expression": <string or null>}}

Set has_computation to false if the question is conceptual / has no single computable numeric answer.
"""

_TF_EXTRACT_PROMPT = """You are checking the numeric correctness of a True/False statistics exam statement.

Statement:
{statement}

If deciding whether the statement is True or False requires computing a numeric value and comparing it to a threshold, extract BOTH the computed quantity as a pure Python arithmetic expression AND the comparison the statement asserts. You may use: + - * / ** ( ) and the functions comb(n, k), factorial(n), exp(x), sqrt(x), log(x), and the constants pi, e.

The comparison operator is one of: "lt", "le", "gt", "ge", "eq" — describing what the STATEMENT claims about (computed_value OPERATOR threshold).

Examples:
  "P(X=3) for lambda=5 is greater than 0.1"
    → expression: "5**3 * exp(-5) / factorial(3)", operator: "gt", threshold: 0.1
  "The variance 2*3 equals 5"
    → expression: "2*3", operator: "eq", threshold: 5

Respond ONLY as valid JSON:
{{"has_computation": <bool>, "expression": <string or null>, "operator": <string or null>, "threshold": <number or null>}}

Set has_computation to false if the statement makes no computable numeric claim.
"""

# Pull the first numeric value out of an option's text (handles plain decimals,
# scientific notation, and a leading approximation/equals sign).
_OPTION_NUMBER = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _option_value(text: str) -> float | None:
    match = _OPTION_NUMBER.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _within_tol(a: float, b: float) -> bool:
    denom = max(abs(b), 1e-12)
    return abs(a - b) / denom <= RELATIVE_TOLERANCE


def _format_value(value: float) -> str:
    """Format a computed value compactly (3-4 sig figs, no trailing zeros)."""
    text = f"{float(f'{value:.4g}'):g}"
    return text


async def _verify_mcq_numeric_one(question: dict) -> None:
    from app.services.question_generator import _MCQ_LETTERS, _split_mcq_text

    stem, options = _split_mcq_text(question.get("question_text", ""))
    correct = (question.get("correct_answer") or "").strip().upper()
    if not stem or correct not in options or len(options) < 3:
        return

    raw = await llm_service.generate(_MCQ_EXTRACT_PROMPT.format(
        stem=stem, options_block=_options_block(options),
    ))
    verdict = _parse_json(raw)
    if not verdict or not verdict.get("has_computation"):
        return
    computed = evaluate_expression(verdict.get("expression") or "")
    if computed is None:
        return

    # Map each option to its numeric value.
    option_values = {l: _option_value(t) for l, t in options.items()}
    matches = [l for l, v in option_values.items() if v is not None and _within_tol(v, computed)]

    if matches:
        # Prefer the option whose value is closest to the computed value.
        best = min(matches, key=lambda l: abs(option_values[l] - computed))
        if best != correct:
            logger.info(
                "[VERIFY-NUM-MCQ] %r: correct_answer %s → %s (computed %s)",
                stem[:80], correct, best, computed,
            )
            question["correct_answer"] = best
        question["model_answer"] = f"{best}. {options[best]}"
        return

    # No option matches the computed value — overwrite the currently-marked
    # option's text with the correct value so exactly one option is right and
    # correct_answer keeps pointing at it.
    new_text = _format_value(computed)
    if option_values.get(correct) is not None and _within_tol(option_values[correct], computed):
        return  # already correct (shouldn't reach here, but be safe)
    logger.info(
        "[VERIFY-NUM-MCQ] %r: no option matched computed %s — rewriting option %s",
        stem[:80], computed, correct,
    )
    options[correct] = new_text
    present = [l for l in _MCQ_LETTERS if l in options]
    question["question_text"] = "\n".join(
        [stem, *(f"{letter}. {options[letter]}" for letter in present)]
    )
    question["model_answer"] = f"{correct}. {new_text}"


_TF_OPS = {
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "eq": lambda a, b: _within_tol(a, b),
}


async def _verify_tf_numeric_one(question: dict) -> None:
    statement = question.get("question_text", "")
    if not statement:
        return
    raw = await llm_service.generate(_TF_EXTRACT_PROMPT.format(statement=statement))
    verdict = _parse_json(raw)
    if not verdict or not verdict.get("has_computation"):
        return
    computed = evaluate_expression(verdict.get("expression") or "")
    operator = (verdict.get("operator") or "").strip().lower()
    threshold = verdict.get("threshold")
    if computed is None or operator not in _TF_OPS or threshold is None:
        return
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return

    truth = "True" if _TF_OPS[operator](computed, threshold) else "False"
    current = (question.get("correct_answer") or "").strip().title()
    if current == truth:
        return
    logger.info(
        "[VERIFY-NUM-TF] %r: correct_answer %s → %s (computed %s %s %s)",
        statement[:80], current or "<unset>", truth, computed, operator, threshold,
    )
    question["correct_answer"] = truth
    # Make the model answer consistent with the corrected key.
    question["model_answer"] = (
        f"{truth}. The computed value is {_format_value(computed)}."
    )


async def verify_objective_numeric(questions: list[dict]) -> list[dict]:
    """Recompute numeric answers for objective (mcq / true_false) questions and
    fix any wrong correct_answer / option text. Non-fatal."""
    targets = [
        q for q in questions
        if q.get("question_type") in ("mcq", "true_false")
    ]
    if not targets:
        return questions
    logger.info(f"[VERIFY-NUM] checking {len(targets)} objective questions for numeric correctness")
    semaphore = asyncio.Semaphore(3)

    async def _bounded(q: dict) -> None:
        async with semaphore:
            try:
                if q.get("question_type") == "mcq":
                    await _verify_mcq_numeric_one(q)
                else:
                    await _verify_tf_numeric_one(q)
            except Exception as exc:
                logger.warning(f"[VERIFY-NUM] check failed (non-fatal): {exc}")

    await asyncio.gather(*[_bounded(q) for q in targets])
    return questions


# ── Question-quality gate ───────────────────────────────────────────────────────
# Reject (DROP from the returned list) questions that are un-renderable,
# incomplete, unanswerable, or whose stated answer is wrong — so the orchestrator
# / ingest top-up loops regenerate replacements rather than show garbage to a
# student. Two stages:
#   A. deterministic renderability checks (pure functions, no LLM) — run first.
#   B. a deepsearch answerability + correctness LLM judge — run on survivors.
# Both are gated by config flags; B is non-fatal per item (infra error → keep).

# A stem that names a table/figure but doesn't include it can't be answered.
_TABLE_REF_RE = re.compile(
    r"\bthe (?:following |data )?table\b|\bfollowing table\b|\bin the table\b|"
    r"\btable\s+\d+\b|\bthe data (?:below|above)\b|\bdata (?:in|from) the table\b|"
    r"\bthe table (?:below|above)\b",
    re.IGNORECASE,
)
# Deliberately demonstrative — requires below/above, a number, "in the", "shown",
# etc. so a generic "graph the function" / "the graph of f(x)" is NOT flagged.
_FIGURE_REF_RE = re.compile(
    r"\bshown (?:below|above|in the figure)\b|"
    r"\bthe (?:figure|graph|chart|plot|histogram|diagram)\s+(?:below|above)\b|"
    r"\b(?:figure|graph|chart|plot|exhibit)\s+\d+\b|"
    r"\bin the (?:figure|graph|chart|plot|histogram|diagram)\b|"
    r"\b(?:according to|based on) the (?:figure|graph|chart|plot|diagram)\b",
    re.IGNORECASE,
)
# Cues that a blank/"?" cell is an intentional "compute this value" prompt.
_FILL_IN_RE = re.compile(
    r"\b(find|compute|calculate|determine|fill in|complete the table|missing|"
    r"the value of|solve for|what is the (?:value|probability|mean|variance))\b",
    re.IGNORECASE,
)
_PLACEHOLDER_MARKERS = ("____", "[blank]", "todo", "<...>", "<…>")
# A trailing connective / operator → the sentence was cut off mid-thought.
_DANGLING_TAIL_RE = re.compile(
    r"(?:,|=|\+|\bthe|\ba|\ban|\bof|\bto|\band|\bor|\bis|\bare|\bwith|\bfor|"
    r"\bwhere|\bwhen|\bwhich|\bthan|\bbecause|\bso that)\s*$",
    re.IGNORECASE,
)


def _table_asset(question: dict) -> dict | None:
    for asset in question.get("assets") or []:
        if isinstance(asset, dict) and asset.get("kind") == "table":
            return asset
    return None


def _figure_asset(question: dict) -> dict | None:
    for asset in question.get("assets") or []:
        if isinstance(asset, dict) and (asset.get("kind") == "figure" or asset.get("image_id")):
            return asset
    return None


def _has_inline_table(text: str) -> bool:
    """True when the stem itself carries a table (HTML or a markdown pipe grid)."""
    if "<table" in text.lower():
        return True
    pipe_lines = [ln for ln in text.splitlines() if ln.count("|") >= 2]
    return len(pipe_lines) >= 2


def _table_content_ok(question: dict, text: str, fill_in_ok: bool) -> bool:
    """A referenced table is renderable if a well-formed table is present either as
    an attached asset or inline in the stem (consistent grid, ≥2 cols, no stray
    '?' unless the question asks the student to compute that cell)."""
    asset = _table_asset(question)
    if asset is not None:
        html = (asset.get("table_html") or "")
        if "<table" not in html.lower():
            return False  # unparseable → rendered as <pre>, i.e. garbled
        if not fill_in_ok and "?" in html:
            return False  # stray placeholder cell with no compute-this instruction
        return True
    if _has_inline_table(text):
        from app.services.question_assets import render_table_html
        pipe_block = "\n".join(ln for ln in text.splitlines() if ln.count("|") >= 2)
        html, n_blanks = render_table_html(pipe_block) if pipe_block else (text, 0)
        if "<table" not in html.lower():
            return False
        if n_blanks and not fill_in_ok:
            return False
        return True
    return False


def _delimiters_balanced(text: str) -> bool:
    has_latex = bool(re.search(r"\\[a-zA-Z]+", text))
    # Odd '$' only matters when LaTeX is clearly present (avoids currency like $25).
    if has_latex and text.count("$") % 2 != 0:
        return False
    if text.count("(") != text.count(")"):
        return False
    if has_latex and text.count("{") != text.count("}"):
        return False
    return True


def _passes_renderability(question: dict) -> tuple[bool, str]:
    """Deterministic, LLM-free gate. Returns (keep, reason). Conservative —
    only flags clear failures. Pure function (unit-testable)."""
    text = (question.get("question_text") or "").strip()
    if not text:
        return False, "empty question text"

    low = text.lower()
    if "____" in text:
        return False, "blank placeholder ____"
    for marker in _PLACEHOLDER_MARKERS:
        if marker in low:
            return False, f"placeholder marker {marker!r}"

    fill_in_ok = bool(_FILL_IN_RE.search(text))
    if re.search(r"[=:]\s*\?", text) and not fill_in_ok:
        return False, "stray '?' used as a value"

    if not _delimiters_balanced(text):
        return False, "unbalanced delimiters ($ / parens / braces)"
    if _DANGLING_TAIL_RE.search(text):
        return False, "truncated (ends mid-sentence)"

    if _TABLE_REF_RE.search(text) and not _table_content_ok(question, text, fill_in_ok):
        return False, "references a table that is missing or garbled"

    if _FIGURE_REF_RE.search(text) and _figure_asset(question) is None:
        return False, "references a figure/graph with no figure asset"

    if question.get("question_type") == "mcq":
        from app.services.question_generator import _split_mcq_text
        _stem, options = _split_mcq_text(text)
        non_empty = {k: v for k, v in options.items() if v and v.strip()}
        if len(non_empty) < 2:
            return False, "MCQ has fewer than 2 options"
        seen_texts = [v.strip().lower() for v in non_empty.values()]
        if len(set(seen_texts)) < len(seen_texts):
            return False, "MCQ has duplicate options"
        correct = (question.get("correct_answer") or "").strip().upper()
        if correct and correct not in non_empty:
            return False, f"MCQ correct_answer {correct!r} not among options"

    return True, ""


_QUALITY_JUDGE_PROMPT = """You are a strict exam-quality reviewer deciding whether a generated question is good enough to show a student.

QUESTION (type: {qtype}):
{stem}

{answer_block}

ATTACHED ASSETS (the student sees these together with the question text):
{asset_block}

SOURCE CONTEXT (textbook excerpts the question was generated from):
{context}

Judge four things, each strictly:
1. self_contained: TRUE if a student can answer using ONLY the question text shown above PLUS any table/figure listed under ATTACHED ASSETS. Treat an attached asset as fully visible to the student: a question that says "the table below" / "the figure below" IS self-contained whenever a complete matching table or figure is attached above. FALSE only if it refers to a table, figure, dataset, or value that is NOT present in the question text AND NOT in ATTACHED ASSETS.
2. answerable_from_source: TRUE if the question is well-posed and answerable (using the attached asset if one is present), and consistent with the source context above.
3. answer_correct: TRUE if the stated model answer / correct option is actually correct for this question (given the attached asset) according to the source context.
4. tests_a_meaningful_concept: TRUE if the question assesses a genuine statistical concept, method, or interpretation. FALSE for dataset trivia / pure lookup — merely reading one value from a table or reciting an incidental narrative detail (a name, place, or one-off count) with no underlying statistical skill.

Respond ONLY as valid JSON:
{{"self_contained": <bool>, "answerable_from_source": <bool>, "answer_correct": <bool>, "tests_a_meaningful_concept": <bool>, "reason": "<short reason>"}}
"""


def _judge_asset_block(question: dict) -> str:
    """Render the question's attached table/figure as text for the judge.

    The judge otherwise only sees question_text, so a legitimate "refer to the
    table/figure below" question (with its asset attached) is wrongly judged
    not-self-contained and dropped. Including the asset content here lets the
    judge evaluate self-containment as the student actually experiences it."""
    parts: list[str] = []
    table = _table_asset(question)
    if table is not None:
        html = (table.get("table_html") or "").strip()
        caption = (table.get("caption") or "").strip()
        if html:
            header = "ATTACHED TABLE" + (f" (caption: {caption})" if caption else "")
            parts.append(f"{header}:\n{html[:2000]}")
    figure = _figure_asset(question)
    if figure is not None:
        desc = (
            figure.get("_figure_spec")
            or figure.get("alt_text")
            or figure.get("caption")
            or ""
        ).strip()
        # The image is generated only AFTER a question passes, so at judge time a
        # figure usually has just its spec. Tell the judge to treat that figure as
        # one the student WILL clearly see, otherwise it wrongly fails self-containment.
        if figure.get("image_id"):
            note = " [a chart image is attached and visible to the student]"
        else:
            note = (" [a clear chart illustrating exactly this WILL be shown to the student directly below the "
                    "question — treat this figure as fully visible when judging self-containment and answerability]")
        if desc or note:
            parts.append(f"ATTACHED FIGURE{note}:\n{desc[:1200]}")
    if not parts:
        return "(No table or figure is attached to this question.)"
    return "\n\n".join(parts)


def _answer_block(question: dict) -> str:
    qtype = question.get("question_type")
    model_answer = question.get("model_answer", "") or ""
    if qtype == "mcq":
        return f"Stated correct option: {question.get('correct_answer', '')}\nModel answer: {model_answer}"
    if qtype == "true_false":
        return f"Stated answer: {question.get('correct_answer', '')}\nModel answer: {model_answer}"
    return f"Model answer: {model_answer}"


async def _judge_question(question: dict) -> tuple[bool, str]:
    """Deepsearch answerability + correctness judge (one LLM call). Returns
    (keep, reason). Non-fatal: any infra/parse failure returns (True, "")."""
    stem = (question.get("question_text") or "").strip()
    if not stem:
        return False, "empty question text"

    try:
        from app.services.llm_service import slm_service
        from app.services.question_generator import DbChunk
        from app.services.retrieval_router import routed_retrieve

        topic = question.get("topic_tag") or ""
        query = f"{topic} {stem}".strip()[:400]
        emb = await slm_service.embed(query)
        k = max(1, int(settings.QUALITY_JUDGE_RETRIEVAL_K))
        fused = await routed_retrieve(
            [query], [emb],
            book_id=question.get("book_id"),
            chapter_num=question.get("chapter_num"),
            k=k,
        )
        chunks = fused.text_chunks[:k]
        context = "\n\n".join(DbChunk(c).to_prompt_block()[:1200] for c in chunks)
    except Exception as exc:
        logger.warning(f"[GATE] judge retrieval failed (non-fatal, keeping): {exc}")
        return True, ""

    if not context.strip():
        # No source evidence retrieved — don't reject on absence of evidence.
        return True, ""

    raw = await llm_service.generate(_QUALITY_JUDGE_PROMPT.format(
        qtype=question.get("question_type", "short_answer"),
        stem=stem,
        answer_block=_answer_block(question),
        asset_block=_judge_asset_block(question),
        context=context[:6000],
    ))
    verdict = _parse_json(raw)
    if not verdict:
        return True, ""  # unparseable judge output → keep (non-fatal)

    fails = []
    if verdict.get("self_contained") is False:
        fails.append("not self-contained")
    if verdict.get("answerable_from_source") is False:
        fails.append("not answerable from source")
    if verdict.get("answer_correct") is False:
        fails.append("answer incorrect")
    if verdict.get("tests_a_meaningful_concept") is False:
        fails.append("dataset trivia / not a meaningful concept")
    if not fails:
        return True, ""
    reason = "; ".join(fails)
    extra = str(verdict.get("reason") or "").strip()
    return False, (f"{reason} — {extra[:160]}" if extra else reason)


async def apply_quality_gate(questions: list[dict]) -> list[dict]:
    """Drop un-renderable / unanswerable / incorrect questions. Returns a
    possibly-SHORTER list so the caller's top-up loop refills the shortfall."""
    if not settings.QUALITY_GATE_ENABLED or not questions:
        return questions

    # Stage A — deterministic renderability (pure, no LLM).
    survivors: list[dict] = []
    for q in questions:
        keep, reason = _passes_renderability(q)
        if keep:
            survivors.append(q)
        else:
            logger.info("[GATE] dropped (renderability: %s): %r", reason, (q.get("question_text", "") or "")[:80])

    # Stage B — deepsearch answerability + correctness judge (LLM, concurrent).
    if not settings.QUALITY_JUDGE_ENABLED or not survivors:
        return survivors

    semaphore = asyncio.Semaphore(max(1, int(settings.QUALITY_JUDGE_CONCURRENCY)))

    async def _bounded(q: dict) -> tuple[dict, bool, str]:
        async with semaphore:
            try:
                keep, reason = await _judge_question(q)
            except Exception as exc:
                logger.warning(f"[GATE] judge failed (non-fatal, keeping): {exc}")
                return q, True, ""
            return q, keep, reason

    results = await asyncio.gather(*[_bounded(q) for q in survivors])
    kept: list[dict] = []
    for q, keep, reason in results:
        if keep:
            kept.append(q)
        else:
            logger.info("[GATE] dropped (judge: %s): %r", reason, (q.get("question_text", "") or "")[:80])
    if len(kept) < len(questions):
        logger.info("[GATE] quality gate kept %d/%d questions", len(kept), len(questions))
    return kept


async def verify_generated_questions(questions: list[dict]) -> list[dict]:
    """All post-generation quality passes: numeric recomputation + MCQ ambiguity
    + math formatting (bare expressions → $-delimited LaTeX for KaTeX), then the
    quality gate which DROPS un-renderable / unanswerable / incorrect questions
    (the returned list may be shorter — top-up loops regenerate the shortfall)."""
    await verify_numeric_model_answers(questions)
    # Fix numerically-wrong objective answers before the ambiguity pass, so the
    # ambiguity judge / distractor rewrites see the corrected correct option.
    await verify_objective_numeric(questions)
    await verify_mcq_options(questions)
    from app.services.math_format import latexify_questions
    await latexify_questions(questions)
    return await apply_quality_gate(questions)
