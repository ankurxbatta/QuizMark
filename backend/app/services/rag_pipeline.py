"""
rag_pipeline.py  —  Hybrid SLM + RAG + LLM marking pipeline.

Tier routing:
  HIGH  (confidence >= CONFIDENCE_HIGH)  → SLM mark accepted, no LLM call
  MID   (CONFIDENCE_MID <= conf < HIGH)  → RAG top-K + offline LLM
  LOW   (confidence < CONFIDENCE_MID)    → RAG wide top-K + online LLM (or offline) + flag
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.config import settings
from app.models.models import Question, Submission
from app.services.llm_service import llm_service, online_service, slm_service, OpenAIClient
from app.services.slm_scorer import slm_pre_score, SLMResult


_MARKING_PROMPT = """You are an expert statistics tutor marking a student answer.

Question: {question_text}

Model Answer:
{model_answer}

Marking Rubric:
{rubric}

Maximum Marks: {max_marks}

Retrieved similar answers for context:
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
        # Strip control characters that break JSON parsing.
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


def _extract_mcq_options(text: str) -> dict[str, str]:
    pattern = re.compile(r"([A-D])[).:\-]\s*")
    matches = list(pattern.finditer(text))
    options: dict[str, str] = {}
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        option_text = text[start:end].strip()
        if option_text:
            options[match.group(1).upper()] = option_text
    return options


def _extract_correct_mcq_answer(question_text: str, model_answer: str) -> str | None:
    match = re.search(r"correct option:\s*([A-D])", model_answer, re.IGNORECASE)
    if match:
        letter = match.group(1).upper()
        options = _extract_mcq_options(question_text)
        return options.get(letter, letter)

    match = re.search(r"correct option:\s*(.+)$", model_answer, re.IGNORECASE | re.MULTILINE)
    if match:
        return match.group(1).strip().strip(".")
    return None


def _extract_true_false_answer(model_answer: str) -> str | None:
    match = re.search(r"\b(true|false)\b", model_answer, re.IGNORECASE)
    return match.group(1).capitalize() if match else None


async def _retrieve_context(db: AsyncSession, emb: list[float], k: int) -> str:
    result = await db.execute(
        text(
            """
            SELECT q.question_text, q.model_answer, q.rubric,
                   1 - (q.embedding <=> CAST(:emb AS vector)) AS similarity
            FROM questions q
            ORDER BY q.embedding <=> CAST(:emb AS vector)
            LIMIT :k
            """
        ),
        {"emb": str(emb), "k": k},
    )
    rows = result.fetchall()
    if not rows:
        return "No similar answers available."
    return "\n\n".join(
        f"[{i}] sim={(r.similarity or 0.0):.2f}\nQ: {r.question_text}\n"
        f"Model: {r.model_answer}\nRubric: {r.rubric}"
        for i, r in enumerate(rows, 1)
    )


async def mark_submission(submission_id: str, db: AsyncSession) -> dict:
    submission = await db.get(Submission, submission_id)
    if not submission:
        raise ValueError(f"Submission {submission_id} not found")
    question: Question = await db.get(Question, submission.question_id)
    if not question:
        raise ValueError(f"Question {submission.question_id} not found")

    # ── Fast path for objective questions ───────────────────────────────────
    if question.question_type in {"mcq", "true_false"}:
        student_answer = submission.answer_text.strip()
        if question.question_type == "mcq":
            correct = _extract_correct_mcq_answer(question.question_text, question.model_answer)
            is_correct = correct and student_answer.lower() == correct.lower()
        else:
            correct = _extract_true_false_answer(question.model_answer)
            is_correct = correct and student_answer.lower() == correct.lower()

        mark = float(question.max_marks if is_correct else 0.0)
        feedback = "Correct." if is_correct else "Incorrect."
        slm = SLMResult(
            keyword_coverage=1.0 if is_correct else 0.0,
            semantic_similarity=1.0 if is_correct else 0.0,
            slm_raw_score=1.0 if is_correct else 0.0,
            confidence=1.0 if is_correct else 0.0,
            provisional_mark=mark,
            route="HIGH",
        )
        _persist(submission, mark, feedback, False, slm)
        await db.commit()
        return _result(mark, feedback, False, slm)

    # ── Tier 1: SLM pre-scorer ────────────────────────────────────────────────
    slm: SLMResult = await slm_pre_score(
        question_text=question.question_text,
        model_answer=question.model_answer,
        rubric=question.rubric,
        max_marks=question.max_marks,
        student_answer=submission.answer_text,
        model_answer_embedding=(
            list(question.embedding) if question.embedding is not None else None
        ),
    )

    # ── HIGH: accept SLM result ───────────────────────────────────────────────
    if slm.route == "HIGH":
        feedback = (
            f"Answer covers {slm.keyword_coverage:.0%} of key rubric terms "
            f"with strong semantic alignment to the model answer."
        )
        _persist(submission, slm.provisional_mark, feedback, False, slm)
        await db.commit()
        return _result(slm.provisional_mark, feedback, False, slm)

    # ── MID / LOW: RAG retrieval ──────────────────────────────────────────────
    answer_emb = await slm_service.embed(submission.answer_text)
    k = settings.TOP_K_RETRIEVAL if slm.route == "MID" else settings.TOP_K_WIDE_RETRIEVAL
    context = await _retrieve_context(db, answer_emb, k)

    prompt = _MARKING_PROMPT.format(
        question_text=question.question_text,
        model_answer=question.model_answer,
        rubric=question.rubric,
        max_marks=question.max_marks,
        context=context,
        student_answer=submission.answer_text,
    )

    # ── Tier 3: LLM selection ─────────────────────────────────────────────────
    use_online = slm.route == "LOW" and online_service is not None
    if use_online:
        try:
            raw = await online_service.generate(prompt)
        except Exception:
            if settings.OPENAI_API_KEY:
                try:
                    raw = await OpenAIClient(model=settings.ONLINE_LLM_MODEL).generate(prompt)
                except Exception:
                    raw = await llm_service.generate(prompt)
            else:
                raw = await llm_service.generate(prompt)
    else:
        raw = await llm_service.generate(prompt)

    res = _parse_llm_json(raw, question.max_marks)
    if slm.route == "LOW":
        res["flagged"] = True  # LOW always flagged

    _persist(submission, res["mark"], res["feedback"], res["flagged"], slm)
    await db.commit()
    return _result(res["mark"], res["feedback"], res["flagged"], slm)


def _persist(sub: Submission, mark: float, feedback: str, flagged: bool, slm: SLMResult):
    sub.auto_mark = mark
    sub.auto_feedback = f"[Route:{slm.route}|Conf:{slm.confidence:.2f}] {feedback}"
    sub.auto_confidence = slm.confidence
    sub.marking_route = slm.route
    sub.slm_keyword_coverage = slm.keyword_coverage
    sub.slm_semantic_sim = slm.semantic_similarity
    sub.slm_raw_score = slm.slm_raw_score
    sub.is_flagged = flagged
    sub.is_marked = True
    sub.marked_at = datetime.utcnow()


def _result(mark: float, feedback: str, flagged: bool, slm: SLMResult) -> dict:
    return {
        "mark": mark,
        "feedback": feedback,
        "flagged": flagged,
        "route": slm.route,
        "confidence": slm.confidence,
    }
