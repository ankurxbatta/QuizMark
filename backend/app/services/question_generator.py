"""
question_generator.py  —  Deep, chunk-aware question generation.

Pipeline:
  1. Receive a list of TextChunk objects from pdf_service.parse_pdf_into_chunks()
  2. Score and rank chunks by teaching value
  3. Group chunks by topic so questions are spread across all chapters
  4. For each topic group, run Two-Stage generation:
       Stage A — SLM (phi3:mini): rapid concept extraction from chunk
       Stage B — LLM (llama3):   rich question construction with rubric
  5. Post-process: validate JSON, deduplicate, assign final metadata
  6. Return sorted list of question dicts ready for DB insertion

For plain-text (.txt) input, falls back to the original single-stage approach.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

from app.services.llm_service import slm_service, llm_service
from app.services.pdf_service import TextChunk


# ─────────────────────────────────────────────────────────────────────────────
#  Prompt templates
# ─────────────────────────────────────────────────────────────────────────────

# Stage A: SLM extracts raw concept skeletons from a single chunk
_SLM_CONCEPT_PROMPT = """\
You are a statistics exam question author reading a textbook section.

SOURCE SECTION:
{chunk_text}

Task: Identify {count} distinct testable concepts from this section.
For each concept output EXACTLY ONE line in this format:
<concept name> | <one-sentence factual answer>

Rules:
- Focus on definitions, formulas, conditions, and interpretations.
- Do NOT copy exercise questions from the text.
- Do NOT output anything except the pipe-separated lines.
"""

# Stage B: LLM turns skeletons into full exam questions
_LLM_ENRICH_PROMPT = """\
You are a statistics assessment author writing exam questions for a business statistics course.

SOURCE CONTEXT (use this to write accurate questions):
{chunk_text}

CONCEPT SKELETONS TO EXPAND:
{skeletons}

QUESTION TYPE REQUIRED: {qtype}

Write each skeleton into a complete, high-quality exam question following these rules:

For short_answer:
- Write a clear, specific question (not "explain" or "describe" vaguely).
- Model answer: 2-5 precise sentences that a student could realistically write.
- Include specific numerical values or formulas where the source text shows them.

For mcq:
- Write an unambiguous question stem.
- Provide exactly 4 options labelled A, B, C, D.
- Only one option is correct; distractors must be plausible but clearly wrong.
- Model answer: state the correct letter and explain why it is correct.

For true_false:
- Write a precise statement that is clearly true OR clearly false.
- Model answer: state "True" or "False" and give a 1-2 sentence justification.

For ALL question types also provide:
- rubric: one criterion per mark, stated as what the student must include.
  Format: "1 mark: <criterion>. 1 mark: <criterion>. ..."
- max_marks: integer (2 for trivial recall, 4 for standard, 6 for analysis, 8 for multi-step)
- topic_tag: the chapter/topic this question comes from (e.g. "Normal Distribution")
- difficulty: easy | medium | hard
  easy   = recall a definition or read a formula
  medium = apply a formula or interpret a result
  hard   = multi-step calculation or critical comparison

Respond ONLY as a valid JSON array. Each element must have these exact keys:
question_text, question_type, model_answer, rubric, max_marks, topic_tag, difficulty

No preamble. No trailing text. Just the JSON array.
"""

# Fallback for plain-text (no chunks)
_PLAIN_TEXT_PROMPT = """\
You are a statistics assessment author.

SOURCE TEXT:
{content}

Generate {count} high-quality exam questions of type "{qtype}".

Rules:
- Questions must be answerable from the source text only.
- For short_answer: 2-5 sentence model answers.
- For mcq: 4 options, one correct, explain why in model_answer.
- For true_false: clear statement + True/False justification.
- Include detailed rubrics (one criterion per mark).
- Spread questions across different concepts in the source.
- max_marks: 2-8 depending on complexity.
- difficulty: easy | medium | hard.

Respond ONLY as a valid JSON array with keys:
question_text, question_type, model_answer, rubric, max_marks, topic_tag, difficulty
"""


# ─────────────────────────────────────────────────────────────────────────────
#  Chunk ranking
# ─────────────────────────────────────────────────────────────────────────────

def _score_chunk(chunk: TextChunk) -> float:
    """
    Score a chunk 0–1 for question-generation value.
    Higher = more teaching content, formulas, examples.
    """
    score = chunk.teaching_density * 0.5
    if chunk.has_formula:
        score += 0.2
    if chunk.has_example:
        score += 0.2
    if len(chunk.key_terms) > 2:
        score += 0.1
    return min(score, 1.0)


def _select_chunks(
    chunks: list[TextChunk],
    count: int,
    topic_filter: Optional[str] = None,
) -> list[TextChunk]:
    """
    Select the best chunks for generation, spread across topics.
    If topic_filter is given, restrict to that topic.
    """
    if topic_filter:
        pool = [c for c in chunks if topic_filter.lower() in c.topic_tag.lower()]
    else:
        pool = chunks

    # Remove low-value chunks
    pool = [c for c in pool if _score_chunk(c) > 0.15]

    if not pool:
        return chunks[:5]  # fallback: return first 5 chunks

    # Sort by score descending
    pool.sort(key=_score_chunk, reverse=True)

    # Group by topic and interleave so we get coverage across chapters
    by_topic: dict[str, list[TextChunk]] = {}
    for c in pool:
        by_topic.setdefault(c.topic_tag, []).append(c)

    # Round-robin across topics to ensure diversity
    selected: list[TextChunk] = []
    topic_iters = {t: iter(cs) for t, cs in by_topic.items()}
    topics = list(topic_iters.keys())
    i = 0
    while len(selected) < count and topic_iters:
        t = topics[i % len(topics)]
        try:
            selected.append(next(topic_iters[t]))
        except StopIteration:
            topics.remove(t)
            del topic_iters[t]
            if not topics:
                break
        i += 1

    return selected[:count]


# ─────────────────────────────────────────────────────────────────────────────
#  Core generation logic
# ─────────────────────────────────────────────────────────────────────────────

async def _slm_extract_concepts(chunk: TextChunk, count: int) -> list[str]:
    """Stage A: use SLM to extract concept skeletons from one chunk."""
    prompt = _SLM_CONCEPT_PROMPT.format(
        chunk_text=chunk.text[:2500],
        count=count,
    )
    try:
        raw = await slm_service.generate(prompt)
        lines = []
        for line in raw.strip().splitlines():
            line = line.strip()
            if "|" in line:
                parts = line.split("|", 1)
                if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                    lines.append(f"- {parts[0].strip()} | {parts[1].strip()}")
        return lines
    except Exception:
        return []


async def _llm_enrich_chunk(
    chunk: TextChunk,
    skeletons: list[str],
    question_type: str,
) -> list[dict]:
    """Stage B: use LLM to build full questions from skeletons + chunk context."""
    if not skeletons:
        return []

    prompt = _LLM_ENRICH_PROMPT.format(
        chunk_text=chunk.to_prompt_block(),
        skeletons="\n".join(skeletons),
        qtype=question_type,
    )
    try:
        raw = await llm_service.generate(prompt)
        return _parse_json_array(raw)
    except Exception:
        return []


async def _generate_from_chunk(
    chunk: TextChunk,
    question_type: str,
    questions_per_chunk: int,
) -> list[dict]:
    """Full two-stage pipeline for a single chunk."""
    # Stage A — SLM concept extraction
    skeletons = await _slm_extract_concepts(chunk, questions_per_chunk)

    if not skeletons:
        # SLM failed — go direct to LLM with chunk context only
        prompt = _PLAIN_TEXT_PROMPT.format(
            content=chunk.to_prompt_block(),
            count=questions_per_chunk,
            qtype=question_type,
        )
        try:
            raw = await llm_service.generate(prompt)
            questions = _parse_json_array(raw)
        except Exception:
            return []
    else:
        # Stage B — LLM enrichment
        questions = await _llm_enrich_chunk(chunk, skeletons, question_type)

    # Stamp correct metadata from the chunk
    for q in questions:
        if not q.get("topic_tag") or q["topic_tag"] == "Unknown":
            q["topic_tag"] = chunk.topic_tag
        q["_source_chunk"] = chunk.label
        q["_page_range"] = f"{chunk.page_start}–{chunk.page_end}"

    return questions


# ─────────────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────────────

async def generate_questions_from_chunks(
    chunks: list[TextChunk],
    question_type: str,
    count: int = 20,
    topic_filter: Optional[str] = None,
) -> list[dict]:
    """
    Generate `count` questions from a list of TextChunk objects.
    Uses two-stage SLM+LLM pipeline per chunk.
    Spreads questions across topics unless topic_filter is set.
    """
    if not chunks:
        return []

    # How many chunks to process and questions per chunk
    # Use more chunks for larger counts to ensure diversity
    num_chunks = min(max(count // 3, 3), len(chunks), 15)
    questions_per_chunk = max(2, (count // num_chunks) + 1)

    selected = _select_chunks(chunks, num_chunks, topic_filter)

    # Process chunks concurrently (up to 3 at a time to avoid OOM on Ollama)
    all_questions: list[dict] = []
    semaphore = asyncio.Semaphore(3)

    async def _bounded(chunk):
        async with semaphore:
            return await _generate_from_chunk(chunk, question_type, questions_per_chunk)

    results = await asyncio.gather(*[_bounded(c) for c in selected])
    for r in results:
        all_questions.extend(r)

    # Deduplicate by question_text similarity (simple prefix check)
    seen: set[str] = set()
    deduped: list[dict] = []
    for q in all_questions:
        key = q.get("question_text", "")[:60].lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(q)

    # Validate required fields
    valid = _validate_questions(deduped, question_type)

    return valid[:count]


async def generate_questions(
    content: str,
    question_type: str,
    count: int = 20,
) -> list[dict]:
    """
    Legacy entry point for plain-text content (non-PDF).
    Uses the original two-stage approach with the full text.
    """
    # Stage A: SLM skeleton extraction from plain text
    slm_prompt = _SLM_CONCEPT_PROMPT.format(
        chunk_text=content[:3000],
        count=count,
    )
    try:
        slm_raw = await slm_service.generate(slm_prompt)
        skeletons = []
        for line in slm_raw.strip().splitlines():
            line = line.strip()
            if "|" in line:
                parts = line.split("|", 1)
                if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                    skeletons.append(f"- {parts[0].strip()} | {parts[1].strip()}")
    except Exception:
        skeletons = []

    if not skeletons:
        # Direct LLM fallback
        prompt = _PLAIN_TEXT_PROMPT.format(
            content=content[:4000],
            count=count,
            qtype=question_type,
        )
        raw = await llm_service.generate(prompt)
        return _validate_questions(_parse_json_array(raw), question_type)[:count]

    # Stage B: LLM enrichment in batches of 15 skeletons
    all_questions: list[dict] = []
    batch_size = 15
    for i in range(0, len(skeletons), batch_size):
        batch = skeletons[i: i + batch_size]
        prompt = _LLM_ENRICH_PROMPT.format(
            chunk_text=content[:2000],
            skeletons="\n".join(batch),
            qtype=question_type,
        )
        raw = await llm_service.generate(prompt)
        all_questions.extend(_parse_json_array(raw))

    return _validate_questions(all_questions, question_type)[:count]


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_json_array(raw: str) -> list[dict]:
    """Extract and parse the first JSON array from raw LLM output."""
    # Strip markdown code fences if present
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        # Try to recover partial JSON
        try:
            partial = match.group().rstrip(",] \n") + "]"
            return json.loads(partial)
        except Exception:
            return []


_REQUIRED_KEYS = {"question_text", "question_type", "model_answer", "rubric", "max_marks"}


def _validate_questions(questions: list[dict], expected_type: str) -> list[dict]:
    """Filter out incomplete or malformed question dicts."""
    valid = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        if not _REQUIRED_KEYS.issubset(q.keys()):
            continue
        if not q.get("question_text", "").strip():
            continue
        if not q.get("model_answer", "").strip():
            continue
        # Normalise question_type
        qt = q.get("question_type", expected_type).lower().replace(" ", "_")
        if qt not in {"short_answer", "mcq", "true_false"}:
            qt = expected_type
        q["question_type"] = qt
        # Normalise max_marks
        try:
            q["max_marks"] = float(q["max_marks"])
        except (TypeError, ValueError):
            q["max_marks"] = 5.0
        # Normalise difficulty
        diff = q.get("difficulty", "medium").lower()
        if diff not in {"easy", "medium", "hard"}:
            diff = "medium"
        q["difficulty"] = diff
        # Default topic_tag
        if not q.get("topic_tag"):
            q["topic_tag"] = "Statistics"
        valid.append(q)
    return valid
