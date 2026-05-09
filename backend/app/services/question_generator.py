"""Question generation service from uploaded text content."""
from app.services.llm_service import llm_service
import json
import re

GENERATION_PROMPT = """
You are a statistics question author. Given the following source text, generate {count} questions of type "{qtype}".

Source Text:
{content}

Rules:
- For MCQ: provide 4 options (A/B/C/D) and the correct answer key.
- For true_false: provide a statement and True or False as the answer.
- For short_answer: provide a question and a model answer (1-3 sentences).
- Include a rubric for each question.
- Assign difficulty: easy, medium, or hard.
- Tag each question with a topic from the source material.

Respond ONLY as a JSON array of objects with keys:
question_text, question_type, model_answer, rubric, max_marks, topic_tag, difficulty
"""


async def generate_questions(content: str, question_type: str, count: int = 20) -> list[dict]:
    prompt = GENERATION_PROMPT.format(content=content[:4000], qtype=question_type, count=count)
    raw = await llm_service.generate(prompt)
    json_match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not json_match:
        raise ValueError("LLM did not return a JSON array")
    return json.loads(json_match.group())
