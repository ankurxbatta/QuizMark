"""
RAG Pipeline:
1. Embed the student answer.
2. Retrieve the top-K similar question+model-answer pairs from pgvector.
3. Build a prompt with the rubric and retrieved context.
4. Call the LLM to produce a mark and feedback.
5. Parse and return structured output.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from app.models.models import Question, Submission
from app.services.llm_service import llm_service
from app.core.config import settings
from datetime import datetime
import json
import re


MARKING_PROMPT = """
You are an expert statistics tutor marking a student's answer.

Question: {question_text}

Model Answer: {model_answer}

Marking Rubric:
{rubric}

Maximum Marks: {max_marks}

Retrieved similar answers for context:
{context}

Student's Answer:
{student_answer}

Instructions:
- Assign a mark between 0 and {max_marks} (decimals allowed).
- Write concise feedback (2-4 sentences) referencing the rubric.
- If confidence is low, set flagged to true.
- Respond ONLY as valid JSON: {{"mark": <float>, "feedback": "<string>", "flagged": <bool>}}
"""


async def mark_submission(submission_id: str, db: AsyncSession) -> dict:
    submission = await db.get(Submission, submission_id)
    if not submission:
        raise ValueError(f"Submission {submission_id} not found")

    question = await db.get(Question, submission.question_id)
    if not question:
        raise ValueError(f"Question {submission.question_id} not found")

    # Embed student answer
    answer_embedding = await llm_service.embed(submission.answer_text)

    # Vector similarity search
    result = await db.execute(
        text(
            """
            SELECT question_text, model_answer
            FROM questions
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :k
            """
        ),
        {"embedding": str(answer_embedding), "k": settings.TOP_K_RETRIEVAL},
    )
    similar = result.fetchall()
    context = "\n".join([f"Q: {r[0]}\nA: {r[1]}" for r in similar])

    prompt = MARKING_PROMPT.format(
        question_text=question.question_text,
        model_answer=question.model_answer,
        rubric=question.rubric,
        max_marks=question.max_marks,
        context=context or "None available",
        student_answer=submission.answer_text,
    )

    raw = await llm_service.generate(prompt)

    # Parse JSON from LLM response
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not json_match:
        raise ValueError(f"LLM did not return parseable JSON: {raw}")

    result_data = json.loads(json_match.group())
    mark = min(float(result_data.get("mark", 0)), question.max_marks)
    feedback = result_data.get("feedback", "")
    flagged = bool(result_data.get("flagged", False))

    # Update submission
    submission.auto_mark = mark
    submission.auto_feedback = feedback
    submission.is_flagged = flagged
    submission.is_marked = True
    submission.marked_at = datetime.utcnow()
    await db.commit()

    return {"mark": mark, "feedback": feedback, "flagged": flagged}
