"""
Regression: a generated MCQ must persist its structured answer key so marking
takes the deterministic exact-comparison path (not the LLM route), AND the API
schema must surface that key instead of silently dropping it.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.schemas.schemas import QuestionOut
from app.services.question_generator import _validate_questions


def _mcq() -> dict:
    return {
        "question_text": (
            "What does the cdf give for a continuous random variable?\n"
            "A. The slope of the pdf\n"
            "B. The area under the pdf up to x\n"
            "C. The mode of the distribution\n"
            "D. The variance of the distribution"
        ),
        "question_type": "mcq",
        "model_answer": "B. The area under the pdf up to x.",
        "rubric": "Full marks: selects the correct option.",
        "max_marks": 2,
        "topic_tag": "Continuous Random Variables",
        "difficulty": "easy",
    }


def test_validate_questions_sets_correct_answer():
    out = _validate_questions([_mcq()], "mcq")
    assert len(out) == 1
    # The model answer points at B, so the stored key must be "B".
    assert out[0]["correct_answer"] == "B"


def test_verification_pipeline_preserves_correct_answer():
    """The post-generation quality passes (numeric + MCQ ambiguity + latex) must
    not drop the answer key set during normalisation."""
    from app.services import answer_verifier as av

    out = _validate_questions([_mcq()], "mcq")
    assert out[0]["correct_answer"] == "B"

    async def fake_gen(prompt: str, *a, **k) -> str:
        # MCQ ambiguity judge: only B is correct → nothing to rewrite.
        if "reviewing a multiple-choice" in prompt:
            return '{"A": false, "B": true, "C": false, "D": false}'
        # Numeric extraction: no computation to verify.
        return '{"has_computation": false}'

    async def run():
        with patch.object(av.llm_service, "generate", new=AsyncMock(side_effect=fake_gen)), \
             patch("app.services.math_format.llm_service.generate", new=AsyncMock(side_effect=fake_gen)):
            return await av.verify_generated_questions(out)

    res = asyncio.run(run())
    assert res[0]["correct_answer"] == "B"


def test_question_out_exposes_correct_answer():
    """QuestionOut must serialise the stored answer key (it used to be dropped,
    making generated MCQs look like correct_answer=null over the API)."""
    out = _validate_questions([_mcq()], "mcq")[0]
    doc = {
        "id": "q-1",
        **{k: out[k] for k in ("question_text", "question_type", "model_answer", "rubric")},
        "max_marks": float(out["max_marks"]),
        "topic_tag": out["topic_tag"],
        "difficulty": out["difficulty"],
        "correct_answer": out["correct_answer"],
        "book_id": "Test_ch4-5",
        "chapter_num": 5,
        "created_at": datetime.now(timezone.utc),
    }
    serialised = QuestionOut(**doc).model_dump()
    assert serialised["correct_answer"] == "B"
    assert serialised["book_id"] == "Test_ch4-5"
    assert serialised["chapter_num"] == 5
