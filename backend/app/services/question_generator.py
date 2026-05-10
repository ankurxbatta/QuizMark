"""
question_generator.py  —  Two-stage question generation.

Stage 1 (SLM):  phi3:mini extracts key concepts and generates a
                raw skeleton (question + 1-line answer) very quickly.

Stage 2 (LLM):  llama3 enriches each skeleton with a detailed model
                answer, a multi-criteria rubric, marks, topic tag,
                and difficulty level.

This produces better questions than asking a single model to do
everything in one pass, and is faster than sending the whole
document to the large model.
"""
import json
import re
from app.services.llm_service import slm_service, llm_service

# ── Stage 1: SLM skeleton prompt ─────────────────────────────────────────────
_SLM_SKELETON_PROMPT = """You are a question author.
Read the text below and list {count} key concepts suitable for a {qtype} question.
For each concept output ONE line: <concept> | <one-sentence answer>
Nothing else.

Text:
{content}"""

# ── Stage 2: LLM enrichment prompt ───────────────────────────────────────────
_LLM_ENRICH_PROMPT = """You are a statistics assessment author.
Expand each skeleton below into a full exam question.

Question type: {qtype}
Skeletons:
{skeletons}

Rules:
- For mcq: provide a clear question stem, 4 options (A/B/C/D), and the correct key.
- For true_false: write a precise statement and state True or False as the answer.
- For short_answer: write a clear question and a 2-4 sentence model answer.
- Include a multi-criteria rubric (1 mark per criterion, total = max_marks).
- Set max_marks to 3 (easy), 5 (medium), or 8 (hard) depending on depth required.
- Tag topic_tag from the source material (e.g. "Hypothesis Testing").
- Set difficulty: easy | medium | hard.

Respond ONLY as a valid JSON array of objects with keys:
question_text, question_type, model_answer, rubric, max_marks, topic_tag, difficulty
"""


async def generate_questions(
    content: str,
    question_type: str,
    count: int = 20,
) -> list[dict]:
    """
    Two-stage generation:
      1. SLM extracts {count} concept skeletons from the source text.
      2. LLM enriches each skeleton into a full exam question with rubric.
    Returns a list of question dicts ready to INSERT into the DB.
    """
    # ── Stage 1: SLM skeleton extraction ─────────────────────────────────────
    # Use the first 4,000 chars for SLM (fast, cheap context window)
    slm_prompt = _SLM_SKELETON_PROMPT.format(
        count=count,
        qtype=question_type,
        content=content[:4000],
    )
    slm_raw = await slm_service.generate(slm_prompt)

    # Parse pipe-separated lines
    skeletons = []
    for line in slm_raw.strip().splitlines():
        line = line.strip()
        if "|" in line:
            parts = line.split("|", 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                skeletons.append(f"- {parts[0].strip()} | {parts[1].strip()}")

    if not skeletons:
        # Fallback: skip stage 1 and send raw content straight to LLM
        return await _fallback_single_stage(content, question_type, count)

    # ── Stage 2: LLM enrichment ───────────────────────────────────────────────
    # Feed up to 30 skeletons at once to stay within LLM context window
    batch_size = 30
    all_questions: list[dict] = []

    for i in range(0, len(skeletons), batch_size):
        batch = skeletons[i : i + batch_size]
        llm_prompt = _LLM_ENRICH_PROMPT.format(
            qtype=question_type,
            skeletons="\n".join(batch),
        )
        llm_raw = await llm_service.generate(llm_prompt)
        parsed = _parse_json_array(llm_raw)
        all_questions.extend(parsed)

    return all_questions[:count]


async def _fallback_single_stage(
    content: str,
    question_type: str,
    count: int,
) -> list[dict]:
    """Single-pass fallback when SLM produces no usable skeletons."""
    prompt = _LLM_ENRICH_PROMPT.format(
        qtype=question_type,
        skeletons=f"Generate {count} questions from this text:\n{content[:3000]}",
    )
    raw = await llm_service.generate(prompt)
    return _parse_json_array(raw)[:count]


def _parse_json_array(raw: str) -> list[dict]:
    """Extract and parse the first JSON array from raw LLM output."""
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return []
