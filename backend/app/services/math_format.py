"""
math_format.py — convert bare, hard-to-read math in generated questions into
properly delimited LaTeX so the frontend can render it with KaTeX.

Generated text often contains undelimited math fragments such as
``P(x) = μ^x e^{-μ} / x!`` or ``q^(n-k)`` mixed into prose. KaTeX can only
render math wrapped in delimiters ($...$), so this module rewrites such text,
wrapping every expression in ``$...$`` and converting unicode/loose notation to
real LaTeX commands. The rewrite is translation-only (an LLM is reliable at
formatting, not arithmetic): wording, numbers and structure are preserved, and
every result is sanity-checked before it is accepted.

Applied as a post-generation pass (verify_generated_questions) and by the
one-off backfill over already-stored questions. Fully non-fatal: any failure
leaves the original text untouched.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re

from app.services.llm_service import llm_service

logger = logging.getLogger(__name__)

LATEX_CACHE_COLLECTION = "latex_cache"

# Signals that a string contains undelimited math worth converting.
_BARE_MATH = re.compile(
    r"\^\{|\^[\-(]?[\dA-Za-zμσλπ]|_\{|\\frac|\\sqrt|\be\^|\bP\s*\(\s*[Xx]"
    r"|C\(\s*\d|[A-Za-z0-9]\)\s*\^|μ|σ|λ|≈|≤|≥|·|×|√"
    r"|\b[A-Za-z]\^[A-Za-z0-9]|/\s*[A-Za-z]!|[A-Za-z0-9]!",
    re.IGNORECASE,
)


def needs_latexify(text: str) -> bool:
    """True for text that has bare math and is not already $-delimited."""
    if not text or not text.strip():
        return False
    if "$" in text:  # assume already converted / intentionally delimited
        return False
    return bool(_BARE_MATH.search(text))


_PLAIN_PROMPT = """Rewrite the exam text below so every mathematical expression is valid LaTeX wrapped in single dollar signs for inline math ($...$).

STRICT RULES:
- Do NOT change any wording, numbers, names, variable letters, or the meaning. Only format the math.
- Convert unicode/Greek to LaTeX commands: μ→\\mu, σ→\\sigma, λ→\\lambda, π→\\pi, ≈→\\approx, ≤→\\leq, ≥→\\geq, ×→\\times, ·→\\cdot, √→\\sqrt.
- Use \\frac{{}}{{}} for fractions, ^{{}} for superscripts, _{{}} for subscripts, \\cdot for multiplication, \\binom{{n}}{{k}} for combinations written C(n, k); factorials stay as n!.
- Keep all prose, punctuation, names and line breaks exactly as they are. Wrap ONLY the math fragments.
- If there is no real math, return the text unchanged.

Return ONLY the rewritten text — no preamble, no explanation, no code fences.

TEXT:
{text}
"""


def _strip_fences(raw: str) -> str:
    out = raw.strip()
    out = re.sub(r"^```[a-zA-Z]*\s*", "", out)
    out = re.sub(r"\s*```$", "", out)
    return out.strip()


def _sanity_ok(original: str, converted: str) -> bool:
    """Reject rewrites that dropped content, added none, or ballooned."""
    if not converted or "$" not in converted:
        return False
    o, c = len(original), len(converted)
    if c < o * 0.6 or c > o * 3 + 120:
        return False
    return True


async def _cache_get(key: str) -> str | None:
    try:
        from app.services.mongo_vector_store import _get_collection
        col = await _get_collection(LATEX_CACHE_COLLECTION)
        doc = await col.find_one({"_id": key})
        return doc.get("latex") if doc else None
    except Exception:
        return None


async def _cache_put(key: str, value: str) -> None:
    try:
        from datetime import datetime, timezone
        from app.services.mongo_vector_store import _get_collection
        col = await _get_collection(LATEX_CACHE_COLLECTION)
        await col.replace_one(
            {"_id": key},
            {"_id": key, "latex": value, "created_at": datetime.now(timezone.utc)},
            upsert=True,
        )
    except Exception:
        pass


async def latexify(text: str) -> str:
    """Return `text` with bare math wrapped in $...$ LaTeX. No-op when not needed."""
    if not needs_latexify(text):
        return text
    key = "tex:" + hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
    cached = await _cache_get(key)
    if cached is not None:
        return cached
    try:
        converted = _strip_fences(await llm_service.generate(_PLAIN_PROMPT.format(text=text)))
    except Exception as exc:
        logger.warning(f"[latex] conversion failed (keeping original): {exc}")
        return text
    if not _sanity_ok(text, converted):
        logger.debug("[latex] rewrite failed sanity check, keeping original")
        return text
    await _cache_put(key, converted)
    return converted


async def latexify_question(question: dict) -> None:
    """Latexify a question dict in place.

    For MCQ the stem and options are converted independently and reassembled so
    the strict ``Stem / A. … / B. …`` structure the parser and marking rely on
    is preserved exactly.
    """
    qtype = question.get("question_type")
    model_answer = question.get("model_answer", "") or ""
    if model_answer:
        question["model_answer"] = await latexify(model_answer)

    text = question.get("question_text", "") or ""
    if qtype == "mcq":
        from app.services.question_generator import _split_mcq_text
        stem, options = _split_mcq_text(text)
        if stem and options:
            new_stem = await latexify(stem)
            new_opts: dict[str, str] = {}
            for letter in sorted(options):
                new_opts[letter] = await latexify(options[letter])
            question["question_text"] = "\n".join(
                [new_stem, *(f"{letter}. {new_opts[letter]}" for letter in sorted(new_opts))]
            )
            return
    if text:
        question["question_text"] = await latexify(text)


async def latexify_questions(questions: list[dict]) -> list[dict]:
    """Convert bare math to LaTeX across a batch, in place. Bounded + non-fatal."""
    targets = [
        q for q in questions
        if needs_latexify(q.get("question_text", "")) or needs_latexify(q.get("model_answer", ""))
    ]
    if not targets:
        return questions
    logger.info(f"[latex] formatting math in {len(targets)}/{len(questions)} questions")
    sem = asyncio.Semaphore(3)

    async def _bounded(q: dict) -> None:
        async with sem:
            try:
                await latexify_question(q)
            except Exception as exc:
                logger.warning(f"[latex] question formatting failed (non-fatal): {exc}")

    await asyncio.gather(*[_bounded(q) for q in targets])
    return questions


async def backfill_stored_questions(book_id: str | None = None) -> dict:
    """One-off backfill: latexify already-stored questions in the DB.

    Questions generated before this pass existed keep their bare math; this walks
    the ``questions`` collection (optionally scoped to one book), formats every
    document that still needs it and writes back only the changed fields. Bounded
    and non-fatal, like the generation-time pass.
    """
    from app.core.database import get_mongo_db
    db = get_mongo_db()
    query: dict = {"book_id": book_id} if book_id else {}
    projection = {"question_text": 1, "model_answer": 1, "question_type": 1}
    docs = await db["questions"].find(query, projection).to_list(length=None)
    targets = [
        d for d in docs
        if needs_latexify(d.get("question_text", "")) or needs_latexify(d.get("model_answer", ""))
    ]
    if not targets:
        return {"scanned": len(docs), "needed": 0, "updated": 0}

    logger.info(f"[latex] backfilling {len(targets)}/{len(docs)} stored questions")
    sem = asyncio.Semaphore(3)
    updated = 0

    async def _one(doc: dict) -> None:
        nonlocal updated
        async with sem:
            q = {
                "question_text": doc.get("question_text", "") or "",
                "model_answer": doc.get("model_answer", "") or "",
                "question_type": doc.get("question_type"),
            }
            try:
                await latexify_question(q)
            except Exception as exc:
                logger.warning(f"[latex] backfill formatting failed (non-fatal): {exc}")
                return
            changes = {
                k: q[k] for k in ("question_text", "model_answer")
                if q[k] != (doc.get(k, "") or "")
            }
            if changes:
                await db["questions"].update_one({"_id": doc["_id"]}, {"$set": changes})
                updated += 1

    await asyncio.gather(*[_one(d) for d in targets])
    return {"scanned": len(docs), "needed": len(targets), "updated": updated}
