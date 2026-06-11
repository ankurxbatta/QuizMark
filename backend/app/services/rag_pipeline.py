"""
rag_pipeline.py  —  Hybrid SLM + RAG + LLM marking pipeline (MongoDB backend).

RAG context for marking pulls from two sources:
  1. pdf_chunks   — actual textbook sections (text, tables, formulas, image
                    descriptions) retrieved via multi-query decomposition.
  2. questions    — similar stored Q&A pairs for comparison calibration.

Multi-query retrieval (inspired by Shiksha Copilot):
  Instead of a single embedding lookup on the student answer, we decompose
  the (question + rubric) into 3 concept-level queries, retrieve chunks for
  each in parallel, then deduplicate. This surfaces all relevant textbook
  material even when the student uses different terminology.

Tier routing:
  HIGH  (confidence >= CONFIDENCE_HIGH)  → SLM mark accepted, no LLM call
  MID   (CONFIDENCE_MID <= conf < HIGH)  → RAG top-K  + offline LLM
  LOW   (confidence < CONFIDENCE_MID)    → RAG wide top-K + online LLM + flag
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.services.llm_service import llm_service, online_service, slm_service
from app.services.slm_scorer import slm_pre_score, SLMResult
from app.services.mongo_vector_store import vector_search, search_similar_questions

logger = logging.getLogger(__name__)


_MARKING_PROMPT = """You are an expert statistics tutor marking a student answer.

Question: {question_text}

Model Answer:
{model_answer}

Marking Rubric:
{rubric}

Maximum Marks: {max_marks}

Retrieved Source Context:
{context}

Student's Answer:
{student_answer}

Instructions:
- Assign a mark between 0 and {max_marks} (decimals allowed).
- Write 2-4 sentences of feedback referencing the rubric criteria.
- Set "flagged": true only if the answer is genuinely ambiguous.
- Respond ONLY as valid JSON:
  {{"mark": <float>, "feedback": "<string>", "flagged": <bool>, "confidence": <float 0-1>}}
"""


def _parse_llm_json(raw: str, max_marks: float) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"LLM did not return parseable JSON. Raw: {raw[:300]}")
    payload = match.group()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        cleaned = "".join(
            ch for ch in payload if ch == "\n" or ch == "\r" or ch == "\t" or ord(ch) >= 32
        )
        data = json.loads(cleaned)
    return {
        "mark": min(float(data.get("mark", 0)), max_marks),
        "feedback": str(data.get("feedback", "")),
        "flagged": bool(data.get("flagged", False)),
        "confidence": float(data.get("confidence", 0.5)),
    }


def _extract_mcq_correct(question_text: str, model_answer: str) -> str | None:
    m = re.match(r"^\s*([A-D])[.):\-]\s", model_answer.strip(), re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.search(r"(?:the\s+)?(?:correct\s+)?answer(?:\s+is)?[:\s]+([A-D])\b", model_answer, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.search(r"(?:option\s+|choice\s+)([A-D])\b", model_answer, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.search(r"^\s*([A-D])\s*$", model_answer, re.IGNORECASE | re.MULTILINE)
    if m:
        return m.group(1).upper()
    return None


def _extract_true_false(model_answer: str) -> str | None:
    match = re.search(r"\b(true|false)\b", model_answer, re.IGNORECASE)
    return match.group(1).capitalize() if match else None


def _build_concept_queries(question_text: str, rubric: str) -> list[str]:
    """
    Derive 3 focused retrieval queries from the question and rubric.

    Instead of retrieving with only the student's answer embedding (which may
    use non-standard terminology), we target the underlying concepts directly:
      Q1 — the core topic of the question
      Q2 — the first rubric criterion (most heavily weighted)
      Q3 — the formula or method the question tests (extracted from rubric)
    """
    # Strip rubric down to first criterion
    first_criterion = re.split(r"\.\s*\d+\s*mark", rubric, maxsplit=1)[0][:200].strip()
    # Extract likely formula/method mentions (words before "mark:" entries)
    method_hint = re.sub(r"\d+ mark[s]?:?", "", rubric, flags=re.IGNORECASE)[:150].strip()
    queries = [
        question_text[:200],
        first_criterion or question_text[:200],
        method_hint or question_text[:200],
    ]
    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for q in queries:
        key = q.lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(q)
    return deduped or [question_text[:200]]


async def _retrieve_context(
    answer_emb: list[float],
    question_text: str,
    rubric: str,
    k: int,
) -> str:
    """
    Build marking context from two MongoDB sources using multi-query retrieval.

    Multi-query approach (Shiksha-inspired):
      - Generate 3 concept-level queries from the question + rubric
      - Embed each query and search in parallel
      - Deduplicate results → richer, more complete textbook coverage
    """
    parts: list[str] = []

    # ── Source 1: multi-query textbook chunk retrieval ─────────────────────────
    concept_queries = _build_concept_queries(question_text, rubric)
    concept_embeddings = await asyncio.gather(
        *[slm_service.embed(q) for q in concept_queries]
    )
    k_per_query = max(2, k // len(concept_embeddings))
    raw_batches = await asyncio.gather(
        *[vector_search(emb, k=k_per_query) for emb in concept_embeddings]
    )

    # Deduplicate chunks by _id
    seen_ids: set[str] = set()
    chunks: list[dict] = []
    for batch in raw_batches:
        for chunk in batch:
            cid = chunk.get("_id", "")
            if cid not in seen_ids:
                seen_ids.add(cid)
                chunks.append(chunk)

    for i, chunk in enumerate(chunks[:k], 1):
        section = f"{chunk.get('chapter_title', '')} — {chunk.get('section_title', '')}"
        block = [f"[TEXTBOOK {i}: {section} | pp.{chunk.get('page_start','')}–{chunk.get('page_end','')}]"]
        block.append(chunk.get("text", ""))
        if chunk.get("table_texts"):
            for t in chunk["table_texts"]:
                block.append(f"[TABLE]\n{t}")
        if chunk.get("math_text"):
            block.append(f"[FORMULAS] {chunk['math_text']}")
        if chunk.get("image_texts"):
            block.append("[VISUAL CONTENT]\n" + "\n".join(chunk["image_texts"]))
        parts.append("\n".join(block))

    # ── Source 2: similar stored Q&A pairs ────────────────────────────────────
    similar_qs = await search_similar_questions(answer_emb, k=min(3, k))
    for i, q in enumerate(similar_qs, 1):
        parts.append(
            f"[SIMILAR Q{i}]\n"
            f"Q: {q.get('question_text', '')}\n"
            f"Model: {q.get('model_answer', '')}\n"
            f"Rubric: {q.get('rubric', '')}"
        )

    # ── Source 3: specialist indexes, heuristically routed (MULTI_RAG_DESIGN) ──
    # No extra LLM or embedding calls: intent comes from regex over the question
    # + rubric, and the already-computed concept embedding is reused. The marker
    # gets the canonical formula (or figure/table) to check the student against.
    specialist_block = await _specialist_marking_context(
        question_text, rubric, concept_embeddings[0] if concept_embeddings else answer_emb
    )
    if specialist_block:
        parts.append(specialist_block)

    return "\n\n".join(parts) if parts else "No relevant context available."


async def _specialist_marking_context(
    question_text: str,
    rubric: str,
    query_emb: list[float],
) -> str:
    """Heuristic-routed specialist context for marking. '' when nothing applies."""
    from app.services.retrieval_router import (
        INTENT_COMPUTATIONAL,
        INTENT_VISUAL,
        classify_intent,
    )

    blocks: list[str] = []
    try:
        intent = classify_intent(f"{question_text} {rubric}")
        if intent == INTENT_COMPUTATIONAL and settings.MATH_INDEX_ENABLED:
            from app.services.math_index import render_formulas_block, retrieve_formulas
            block = render_formulas_block(await retrieve_formulas(query_emb, k=3))
            if block:
                blocks.append(block)
        elif intent == INTENT_VISUAL:
            if settings.FIGURE_INDEX_ENABLED:
                from app.services.figure_index import render_figures_block, retrieve_figures
                block = render_figures_block(await retrieve_figures(query_emb, k=2))
                if block:
                    blocks.append(block)
            if settings.TABLE_INDEX_ENABLED:
                from app.services.table_index import render_tables_block, retrieve_tables
                block = render_tables_block(await retrieve_tables(query_emb, k=2))
                if block:
                    blocks.append(block)
    except Exception as exc:
        logger.debug(f"specialist marking context skipped: {exc}")
    return "\n\n".join(blocks)


async def mark_submission(submission_id: str, db: AsyncIOMotorDatabase) -> dict:
    submission = await db["submissions"].find_one({"_id": submission_id})
    if not submission:
        raise ValueError(f"Submission {submission_id} not found")

    question = await db["questions"].find_one({"_id": submission["question_id"]})
    if not question:
        raise ValueError(f"Question {submission['question_id']} not found")

    # ── Fast path for objective questions ─────────────────────────────────────
    if question["question_type"] in {"mcq", "true_false"}:
        student_answer = submission["answer_text"].strip()
        if question["question_type"] == "mcq":
            correct = _extract_mcq_correct(question["question_text"], question["model_answer"])
            is_correct = bool(correct and student_answer.lower() == correct.lower())
        else:
            correct = _extract_true_false(question["model_answer"])
            is_correct = bool(correct and student_answer.lower() == correct.lower())

        mark = float(question["max_marks"] if is_correct else 0.0)
        feedback = "Correct." if is_correct else "Incorrect."
        slm = SLMResult(
            keyword_coverage=1.0 if is_correct else 0.0,
            semantic_similarity=1.0 if is_correct else 0.0,
            slm_raw_score=1.0 if is_correct else 0.0,
            confidence=1.0 if is_correct else 0.0,
            provisional_mark=mark,
            route="HIGH",
        )
        await _persist(db, submission_id, mark, feedback, False, slm)
        return _result(mark, feedback, False, slm)

    # ── Tier 1: SLM pre-scorer ────────────────────────────────────────────────
    embedding = question.get("embedding")
    slm: SLMResult = await slm_pre_score(
        question_text=question["question_text"],
        model_answer=question["model_answer"],
        rubric=question["rubric"],
        max_marks=question["max_marks"],
        student_answer=submission["answer_text"],
        model_answer_embedding=embedding,
    )

    # ── HIGH: accept SLM result ───────────────────────────────────────────────
    if slm.route == "HIGH":
        feedback = (
            f"Answer covers {slm.keyword_coverage:.0%} of key rubric terms "
            f"with strong semantic alignment to the model answer."
        )
        await _persist(db, submission_id, slm.provisional_mark, feedback, False, slm)
        return _result(slm.provisional_mark, feedback, False, slm)

    # ── MID / LOW: RAG retrieval from MongoDB (chunks + questions) ────────────
    answer_emb = await slm_service.embed(submission["answer_text"])
    k = settings.TOP_K_RETRIEVAL if slm.route == "MID" else settings.TOP_K_WIDE_RETRIEVAL
    context = await _retrieve_context(
        answer_emb,
        question_text=question["question_text"],
        rubric=question["rubric"],
        k=k,
    )

    prompt = _MARKING_PROMPT.format(
        question_text=question["question_text"],
        model_answer=question["model_answer"],
        rubric=question["rubric"],
        max_marks=question["max_marks"],
        context=context,
        student_answer=submission["answer_text"],
    )

    # ── Tier 3: Gemini generation (all routes) ────────────────────────────────
    # llm_service and online_service both point to Gemini; use online for LOW
    # (flagged) answers, llm_service for MID/HIGH fallthrough.
    if slm.route == "LOW" and online_service is not None:
        try:
            raw = await online_service.generate(prompt)
        except Exception:
            raw = await llm_service.generate(prompt)
    else:
        raw = await llm_service.generate(prompt)

    try:
        res = _parse_llm_json(raw, question["max_marks"])
    except (ValueError, json.JSONDecodeError):
        res = {
            "mark": 0.0,
            "feedback": "Automated marking could not parse the AI response. Please review manually.",
            "flagged": True,
            "confidence": 0.0,
        }
    if slm.route == "LOW":
        res["flagged"] = True

    await _persist(db, submission_id, res["mark"], res["feedback"], res["flagged"], slm)
    return _result(res["mark"], res["feedback"], res["flagged"], slm)


async def _persist(
    db: AsyncIOMotorDatabase,
    submission_id: str,
    mark: float,
    feedback: str,
    flagged: bool,
    slm: SLMResult,
) -> None:
    await db["submissions"].update_one(
        {"_id": submission_id},
        {"$set": {
            "auto_mark": mark,
            "auto_feedback": f"[Route:{slm.route}|Conf:{slm.confidence:.2f}] {feedback}",
            "auto_confidence": slm.confidence,
            "marking_route": slm.route,
            "slm_keyword_coverage": slm.keyword_coverage,
            "slm_semantic_sim": slm.semantic_similarity,
            "slm_raw_score": slm.slm_raw_score,
            "is_flagged": flagged,
            "is_marked": True,
            "marked_at": datetime.now(timezone.utc),
        }},
    )


def _result(mark: float, feedback: str, flagged: bool, slm: SLMResult) -> dict:
    return {
        "mark": mark,
        "feedback": feedback,
        "flagged": flagged,
        "route": slm.route,
        "confidence": slm.confidence,
    }
