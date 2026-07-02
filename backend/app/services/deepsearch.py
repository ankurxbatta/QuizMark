"""
deepsearch.py — DeepSearch question refiner: the repair pass BEFORE the validator.

Freshly generated questions go through the quality gate (answer_verifier), which
DROPS anything broken — a dropped question costs a whole regeneration round.
DeepSearch sits between generation and that gate and tries to FIX instead of
letting the gate discard:

  1. Evidence — multi-index RAG (routed_retrieve) pulls the question's supporting
     material from every part of the database: pdf_chunks plus the specialist
     math/figure/table indexes.
  2. Web knowledge (optional) — when TAVILY_API_KEY is set, a web search adds
     outside-the-book context so factual claims can be checked even when the
     textbook excerpt is thin.
  3. Critic-repair LLM call — sees the question, the evidence, and the EXACT
     auto-rejection rules the validator enforces (_REJECTION_CRITERIA — the
     single source of truth shared with the generation prompt and the gate).
     It completes what is missing (e.g. attaches a referenced-but-absent table),
     corrects what is wrong (bad numbers, a correct_answer not in the options),
     and rewrites anything the gate would reject.

Fail-open by design: any error — retrieval, web, LLM, unparseable output —
returns the ORIGINAL question unchanged. DeepSearch can only ever improve a
question, never lose one; the validator still has the final word.
"""
import asyncio
import json
import logging
import re

import httpx

from app.core.config import settings
from app.services.llm_service import generation_service, slm_service

logger = logging.getLogger(__name__)

# Fields the repair step is allowed to change. Everything else (ids, embeddings,
# book/chapter scoping, type, source metadata) is preserved from the original so
# a creative LLM cannot corrupt pipeline bookkeeping.
_EDITABLE_FIELDS = (
    "question_text",
    "model_answer",
    "rubric",
    "correct_answer",
    "max_marks",
    "assets",
)

_REPAIR_PROMPT = """\
You are DeepSearch, a question-repair specialist for a statistics exam generator.
A generated question is about to be judged by an automatic validator. Your job:
make the question the best possible version of itself so the validator KEEPS it.

THE QUESTION (JSON):
{question_json}

TEXTBOOK EVIDENCE (retrieved from the book's text, formula, figure and table indexes):
{evidence}

{web_block}
VALIDATOR RULES — the question is DISCARDED if it matches ANY of these:
{rejection_criteria}

YOUR TASK — check and repair, in this order:
1. CORRECTNESS: verify every fact, formula and numeric result in the question,
   options and model_answer against the evidence (and your own statistical
   knowledge). If a number or claim is wrong, correct it — recompute from the
   data the question gives.
2. COMPLETENESS: if the question references data/a table/a figure that is not
   attached, attach it (inline markdown table in question_text, or complete the
   existing entry in "assets"). If the rubric or model_answer is missing or too
   thin to mark against, complete it.
3. VALIDATOR-PROOFING: fix anything the rules above would reject — dangling
   references, book source labels ("Table 1.9"), placeholders, unbalanced math
   delimiters, truncation, MCQ option problems, non-self-containment.
4. Keep the question's intent, type, difficulty and Bloom's level. For MCQs keep
   the exact "Stem?\nA. ...\nB. ...\nC. ...\nD. ..." layout inside question_text
   and make sure correct_answer names one of the options.

OUTPUT — respond with ONLY one JSON object, no prose, no code fences:
- If the question is already correct, complete and validator-proof:
  {{"verdict": "ok"}}
- Otherwise:
  {{"verdict": "repaired", "changes": "<one short line: what you fixed>",
    "question": {{ ...the FULL corrected question object, same keys as input... }}}}
"""


# ─────────────────────────────────────────────────────────────────────────────
#  Web knowledge (optional — inert unless TAVILY_API_KEY is configured)
# ─────────────────────────────────────────────────────────────────────────────

async def web_search(query: str, max_results: int | None = None) -> list[dict]:
    """Tavily web search. Returns [] when disabled, unconfigured, or on any error."""
    key = getattr(settings, "TAVILY_API_KEY", None)
    if not (settings.DEEPSEARCH_WEB_ENABLED and key):
        return []
    n = max_results or settings.DEEPSEARCH_WEB_MAX_RESULTS
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": key,
                    "query": query,
                    "max_results": n,
                    "search_depth": "basic",
                    "include_answer": True,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning(f"[DeepSearch] web search failed (non-fatal): {exc}")
        return []
    results: list[dict] = []
    if data.get("answer"):
        results.append({"title": "Search summary", "content": str(data["answer"])[:800], "url": ""})
    for r in data.get("results", [])[:n]:
        results.append({
            "title": str(r.get("title", ""))[:120],
            "content": str(r.get("content", ""))[:600],
            "url": str(r.get("url", "")),
        })
    return results


def _web_block(results: list[dict]) -> str:
    if not results:
        return ""
    lines = [
        f"[WEB {i}: {r['title']}]\n{r['content']}"
        for i, r in enumerate(results, 1)
    ]
    return "WEB EVIDENCE (outside knowledge — use to verify facts the textbook excerpt doesn't cover):\n" + "\n\n".join(lines) + "\n\n"


# ─────────────────────────────────────────────────────────────────────────────
#  Evidence gathering — every part of the database, not just flat chunks
# ─────────────────────────────────────────────────────────────────────────────

def _stem(question: dict) -> str:
    """The question stem (MCQ options stripped) — the best retrieval/web query."""
    text = (question.get("question_text") or "").strip()
    stem = re.split(r"\n[A-D]\.\s", text)[0]
    return stem[:500]


async def _rag_evidence(question: dict, book_id: str | None, chapter_num: int | None) -> str:
    from app.services.retrieval_router import routed_retrieve

    queries = [_stem(question)]
    model_answer = (question.get("model_answer") or "").strip()
    if model_answer:
        queries.append(model_answer[:400])
    embeddings = await asyncio.gather(*[slm_service.embed(q) for q in queries])
    fused = await routed_retrieve(
        queries, embeddings,
        book_id=book_id, chapter_num=chapter_num,
        k=settings.DEEPSEARCH_RETRIEVAL_K,
    )
    return fused.to_prompt(max_chunks=settings.DEEPSEARCH_RETRIEVAL_K)[:4500]


# ─────────────────────────────────────────────────────────────────────────────
#  Critic-repair
# ─────────────────────────────────────────────────────────────────────────────

def _question_for_prompt(question: dict) -> str:
    """The editable view of the question — no embeddings/ids in the prompt."""
    view = {k: question[k] for k in (
        "question_text", "question_type", "difficulty", "blooms_level",
        "model_answer", "rubric", "correct_answer", "max_marks", "assets",
    ) if k in question}
    return json.dumps(view, ensure_ascii=False, indent=2, default=str)


def _merge_repair(original: dict, repaired: dict) -> dict:
    """Apply the LLM's repaired fields onto the original, conservatively.

    Only _EDITABLE_FIELDS may change; a repair that empties question_text or
    mangles types is rejected wholesale (returns the original).
    """
    text = repaired.get("question_text")
    if not isinstance(text, str) or not text.strip():
        return original
    merged = dict(original)
    for field in _EDITABLE_FIELDS:
        if field not in repaired:
            continue
        value = repaired[field]
        if field == "max_marks":
            if isinstance(value, (int, float)) and value > 0:
                merged[field] = float(value)
        elif field == "assets":
            if isinstance(value, list) and all(isinstance(a, dict) for a in value):
                merged[field] = value
        elif isinstance(value, str):
            merged[field] = value
    return merged


async def _refine_one(question: dict, book_id: str | None, chapter_num: int | None) -> dict:
    evidence = ""
    try:
        evidence = await _rag_evidence(question, book_id, chapter_num)
    except Exception as exc:
        logger.warning(f"[DeepSearch] evidence retrieval failed (continuing without): {exc}")

    web_results: list[dict] = []
    try:
        web_results = await web_search(_stem(question))
    except Exception:
        pass  # web_search already logs; belt-and-braces

    from app.services.question_generator import _REJECTION_CRITERIA, _parse_single_json_obj

    prompt = _REPAIR_PROMPT.format(
        question_json=_question_for_prompt(question),
        evidence=evidence or "(no textbook evidence retrieved)",
        web_block=_web_block(web_results),
        rejection_criteria=_REJECTION_CRITERIA,
    )
    raw = await generation_service.generate(prompt)
    parsed = _parse_single_json_obj(raw)
    if not isinstance(parsed, dict):
        logger.warning("[DeepSearch] unparseable repair output — keeping original")
        return question
    if parsed.get("verdict") == "repaired" and isinstance(parsed.get("question"), dict):
        merged = _merge_repair(question, parsed["question"])
        if merged is not question:
            logger.info(f"[DeepSearch] repaired: {parsed.get('changes', 'unspecified')}")
        return merged
    return question


async def refine_questions(
    questions: list[dict],
    book_id: str | None = None,
    chapter_num: int | None = None,
) -> list[dict]:
    """Refine a batch of freshly generated questions before the quality gate.

    Same-length guarantee: every input question comes back, repaired or
    untouched — DeepSearch never drops; only the validator may drop.
    """
    if not questions or not settings.DEEPSEARCH_REFINE_ENABLED:
        return questions

    sem = asyncio.Semaphore(max(1, int(settings.DEEPSEARCH_CONCURRENCY)))

    async def _guarded(q: dict) -> dict:
        async with sem:
            try:
                return await _refine_one(q, book_id, chapter_num)
            except Exception as exc:
                logger.warning(f"[DeepSearch] refine failed for one question (kept original): {exc}")
                return q

    refined = await asyncio.gather(*[_guarded(q) for q in questions])
    changed = sum(1 for before, after in zip(questions, refined) if before is not after)
    logger.info(f"[DeepSearch] refined {len(questions)} questions — {changed} repaired")
    return list(refined)
