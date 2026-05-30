"""
rag_pipeline.py  —  Hybrid SLM + RAG + LLM marking pipeline (MongoDB backend).

RAG context for marking now pulls from two sources:
  1. pdf_chunks   — actual textbook sections (text, tables, formulas, image
                    descriptions) retrieved by semantic similarity to the
                    student's answer. Gives the LLM the source material.
  2. questions    — similar stored Q&A pairs for comparison calibration.

Tier routing:
  HIGH  (confidence >= CONFIDENCE_HIGH)  → SLM mark accepted, no LLM call
  MID   (CONFIDENCE_MID <= conf < HIGH)  → RAG top-K  + offline LLM
  LOW   (confidence < CONFIDENCE_MID)    → RAG wide top-K + online LLM + flag
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.services.llm_service import llm_service, online_service, slm_service
from app.services.slm_scorer import slm_pre_score, SLMResult
from app.services.mongo_vector_store import vector_search, search_similar_questions


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


async def _retrieve_context(emb: list[float], k: int) -> str:
    """
    Build marking context from two MongoDB sources:
      1. Textbook chunks (primary) — rich content: text, tables, formulas, images
      2. Similar stored Q&A pairs (secondary) — calibration reference
    """
    parts: list[str] = []

    # ── Source 1: textbook chunks (the key RAG improvement) ───────────────────
    chunks = await vector_search(emb, k=k)
    for i, chunk in enumerate(chunks, 1):
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
    similar_qs = await search_similar_questions(emb, k=min(3, k))
    for i, q in enumerate(similar_qs, 1):
        parts.append(
            f"[SIMILAR Q{i}]\n"
            f"Q: {q.get('question_text', '')}\n"
            f"Model: {q.get('model_answer', '')}\n"
            f"Rubric: {q.get('rubric', '')}"
        )

    return "\n\n".join(parts) if parts else "No relevant context available."


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
    context = await _retrieve_context(answer_emb, k)

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

    res = _parse_llm_json(raw, question["max_marks"])
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
