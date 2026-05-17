"""
question_generator.py  —  Deep, chunk-aware question generation.

Pipeline:
  1. Receive a list of TextChunk objects from pdf_service.parse_pdf_into_chunks()
  2. Score and rank chunks by teaching value
  3. Group chunks by topic so questions are spread across all chapters
  4. For each topic group, run Two-Stage generation:
       Stage A — Online LLM: rapid concept extraction from chunk
       Stage B — Online LLM: rich question construction with rubric
  5. Post-process: validate JSON, deduplicate, assign final metadata
  6. Return sorted list of question dicts ready for DB insertion

For plain-text (.txt) input, falls back to the original single-stage approach.

Note: Question generation now uses online LLM (Claude/GPT/Gemini) for better speed and reliability.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

from app.services.llm_service import generation_service
from app.services.pdf_service import TextChunk


def _safe_exception_message(exc: Exception) -> str:
    message = str(exc)
    message = re.sub(r"([?&]key=)[^&\s']+", r"\1***", message)
    message = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1***", message)
    return message


# ─────────────────────────────────────────────────────────────────────────────
#  Prompt templates
# ─────────────────────────────────────────────────────────────────────────────

# Stage A: Online LLM extracts raw concept skeletons from a single chunk
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
- Rubric: single criterion - full marks if the correct option is selected.

For true_false:
- Write a precise statement that is clearly true OR clearly false.
- Model answer: state "True" or "False" and give a 1-2 sentence justification.
- Rubric: single criterion - full marks if the correct truth value is selected.

For ALL question types also provide:
- rubric:
    - short_answer: one criterion per mark, stated as what the student must include.
        Format: "1 mark: <criterion>. 1 mark: <criterion>. ..."
    - mcq/true_false: single criterion that awards full marks for the correct choice.
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
- For short_answer: rubric is one criterion per mark.
- For mcq/true_false: rubric is a single criterion that awards full marks for the correct choice.
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
    textbook_chunks = [c for c in chunks if getattr(c, "chapter_num", 0) > 0]
    source_chunks = textbook_chunks or chunks

    if topic_filter:
        pool = [c for c in source_chunks if topic_filter.lower() in c.topic_tag.lower()]
    else:
        pool = source_chunks

    # Remove low-value chunks
    pool = [c for c in pool if _score_chunk(c) > 0.15]

    if not pool:
        return source_chunks[:5]  # fallback: return first 5 chunks

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
    """Stage A: use online LLM to extract concept skeletons from one chunk."""
    prompt = _SLM_CONCEPT_PROMPT.format(
        chunk_text=chunk.text[:2500],
        count=count,
    )
    try:
        raw = await generation_service.generate(prompt)
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
    """Stage B: use online LLM to build full questions from skeletons + chunk context."""
    if not skeletons:
        return []

    prompt = _LLM_ENRICH_PROMPT.format(
        chunk_text=chunk.to_prompt_block(),
        skeletons="\n".join(skeletons),
        qtype=question_type,
    )
    try:
        raw = await generation_service.generate(prompt)
        return _parse_json_array(raw)
    except Exception:
        return []


async def _generate_from_chunk(
    chunk: TextChunk,
    question_type: str,
    questions_per_chunk: int,
) -> list[dict]:
    """Robust single-stage generation. Direct prompt per chunk, no fragile Stage A dependency."""
    # Single stage: generate directly from chunk content.
    prompt = _PLAIN_TEXT_PROMPT.format(
        content=chunk.to_prompt_block(),
        count=questions_per_chunk,
        qtype=question_type,
    )
    try:
        raw = await generation_service.generate(prompt)
        questions = _parse_json_array(raw)
    except Exception as e:
        print(f"[GEN] chunk generation failed: {_safe_exception_message(e)}")
        questions = []

    if not questions:
        print("[GEN] using deterministic fallback questions for chunk")
        questions = _fallback_questions_from_text(
            chunk.text,
            question_type,
            questions_per_chunk,
            topic_tag=chunk.topic_tag,
            key_terms=chunk.key_terms,
        )

    # Stamp correct metadata from the chunk
    for q in questions:
        if not q.get("topic_tag") or q["topic_tag"] in ("Unknown", "Statistics", ""):
            q["topic_tag"] = chunk.topic_tag
        q["_source_chunk"] = chunk.label
        q["_page_range"] = f"{chunk.page_start}-{chunk.page_end}"

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
    print(f"[GEN] generate_questions_from_chunks: {len(chunks)} chunks, qtype={question_type}, count={count}")
    
    if not chunks:
        print("[GEN] No chunks provided!")
        return []

    # How many chunks to process and questions per chunk
    # Use more chunks for larger counts to ensure diversity
    num_chunks = min(max((count + 2) // 3, 1), len(chunks), 15)
    questions_per_chunk = max(2, (count // num_chunks) + 1)
    
    print(f"[GEN] Processing {num_chunks} chunks, {questions_per_chunk} questions per chunk")

    selected = _select_chunks(chunks, num_chunks, topic_filter)
    print(f"[GEN] Selected {len(selected)} chunks")

    # Process chunks concurrently (up to 3 at a time to avoid OOM on Ollama)
    all_questions: list[dict] = []
    semaphore = asyncio.Semaphore(3)

    async def _bounded(chunk):
        async with semaphore:
            return await _generate_from_chunk(chunk, question_type, questions_per_chunk)

    results = await asyncio.gather(*[_bounded(c) for c in selected])
    for r in results:
        all_questions.extend(r)
    
    print(f"[GEN] Generated {len(all_questions)} questions from all chunks")

    # Deduplicate by question_text similarity (simple prefix check)
    seen: set[str] = set()
    deduped: list[dict] = []
    for q in all_questions:
        key = q.get("question_text", "")[:60].lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(q)
    
    print(f"[GEN] After dedup: {len(deduped)} questions")

    # Validate required fields
    valid = _validate_questions(deduped, question_type)
    print(f"[GEN] After validation: {len(valid)} questions")

    return valid[:count]


async def generate_questions(
    content: str,
    question_type: str,
    count: int = 20,
) -> list[dict]:
    """
    Simplified direct generation without intermediate skeleton extraction.
    Uses the fallback approach that works better with limited resources.
    """
    print(f"[GEN] Starting direct question generation with question_type={question_type}, count={count}")
    print(f"[GEN] Content length: {len(content)} chars")
    
    # Direct LLM generation without skeleton extraction
    # This is more reliable on resource-constrained systems
    prompt = _PLAIN_TEXT_PROMPT.format(
        content=content[:4000],
        count=count,
        qtype=question_type,
    )
    try:
        print("[GEN] Generating questions directly...")
        raw = await generation_service.generate(prompt)
        print(f"[GEN] LLM response length: {len(raw)}")
        all_questions = _parse_json_array(raw)
        print(f"[GEN] Generated {len(all_questions)} questions")
    except Exception as e:
        print(f"[GEN] Direct generation failed: {_safe_exception_message(e)}")
        all_questions = []

    if not all_questions:
        print("[GEN] using deterministic fallback questions for text input")
        all_questions = _fallback_questions_from_text(
            content,
            question_type,
            count,
            topic_tag="Statistics",
        )
    
    result = _validate_questions(all_questions, question_type)[:count]
    print(f"[GEN] Valid after validation: {len(result)}")
    return result


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


_STAT_FALLBACK_TERMS = [
    "mean",
    "median",
    "mode",
    "range",
    "variance",
    "standard deviation",
    "probability",
    "distribution",
    "sample",
    "population",
    "random variable",
    "hypothesis",
    "confidence interval",
    "correlation",
    "regression",
]

_FALLBACK_SKIP_HINTS = (
    "chapter ",
    "figure ",
    " table ",
    "homework",
    "review question",
    "practice test",
    "openstax",
    "download for free",
    "table of contents",
    "appendix",
    "index",
)

_FALLBACK_TERM_SKIP = {
    "chapter",
    "figure",
    "table",
    "example",
    "solution",
    "introduction",
}


def _normalise_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _candidate_sentences(text: str, limit: int = 24) -> list[str]:
    cleaned = _normalise_text(text).replace("•", ". ")
    sentences = []
    for sentence in re.split(r"(?<=[.!?])\s+", cleaned):
        sentence = sentence.strip(" -•\t\n")
        lower = sentence.lower()
        if any(hint in lower for hint in _FALLBACK_SKIP_HINTS):
            continue
        if sentence.count("•") > 1:
            continue
        if "?" in sentence:
            continue
        if len(sentence) < 35 or len(sentence) > 320:
            continue
        if len(sentence.split()) < 6:
            continue
        sentences.append(sentence)
        if len(sentences) >= limit:
            break
    if not sentences and cleaned:
        sentences.append(cleaned[:280].strip())
    return sentences


def _dedupe_terms(terms: list[str]) -> list[str]:
    seen = set()
    deduped = []
    for term in terms:
        term = _normalise_text(term).strip(" .,:;()[]{}")
        key = term.lower()
        if key in _FALLBACK_TERM_SKIP or key.endswith(" topics"):
            continue
        if len(term) < 3 or key in seen:
            continue
        seen.add(key)
        deduped.append(term)
    return deduped


def _terms_from_text(text: str, key_terms: Optional[list[str]] = None) -> list[str]:
    terms = list(key_terms or [])
    lower = text.lower()
    terms.extend(term for term in _STAT_FALLBACK_TERMS if term in lower)
    return _dedupe_terms(terms)


def _choose_term(sentence: str, terms: list[str], index: int) -> str:
    for term in terms:
        if re.search(rf"\b{re.escape(term)}\b", sentence, re.IGNORECASE):
            return term
    return terms[index % len(terms)] if terms else "the concept"


def _cloze_sentence(sentence: str, term: str) -> str:
    return re.sub(rf"\b{re.escape(term)}\b", "____", sentence, count=1, flags=re.IGNORECASE)


def _distractors(correct: str, terms: list[str]) -> list[str]:
    generic = [
        "sampling error",
        "categorical variable",
        "relative frequency",
        "null hypothesis",
        "standard deviation",
    ]
    options = []
    for term in terms + generic:
        if term.lower() == correct.lower():
            continue
        if term.lower() in {o.lower() for o in options}:
            continue
        options.append(term)
        if len(options) == 3:
            break
    return options[:3]


def _fallback_questions_from_text(
    content: str,
    question_type: str,
    count: int,
    topic_tag: str,
    key_terms: Optional[list[str]] = None,
) -> list[dict]:
    sentences = _candidate_sentences(content)
    if not sentences:
        return []

    terms = _terms_from_text(content, key_terms)
    questions = []

    for i in range(count):
        sentence = sentences[i % len(sentences)]
        term = _choose_term(sentence, terms, i)
        if question_type in {"mcq", "true_false"}:
            rubric = "Full marks: selects the correct option."
        else:
            rubric = (
                "1 mark: identifies the relevant concept. "
                "1 mark: explains it consistently with the source text."
            )

        if question_type == "mcq":
            if term != "the concept" and re.search(rf"\b{re.escape(term)}\b", sentence, re.IGNORECASE):
                stem = _cloze_sentence(sentence, term)
                wrong = _distractors(term, terms)
                options = [term] + wrong
                question_text = (
                    "Which term best completes the statement?\n"
                    f"\"{stem}\"\n"
                    f"A. {options[0]}\nB. {options[1]}\nC. {options[2]}\nD. {options[3]}"
                )
                model_answer = f"A. {term}. The source states: {sentence}"
            else:
                question_text = (
                    f"Which statement is supported by the source section on {topic_tag}?\n"
                    f"A. {sentence}\n"
                    "B. The concept applies only when every data value is identical.\n"
                    "C. The concept removes the need to interpret data in context.\n"
                    "D. The concept is unrelated to probability, sampling, or variation."
                )
                model_answer = f"A. The source supports this statement: {sentence}"
        elif question_type == "true_false":
            statement = sentence if sentence.endswith(".") else f"{sentence}."
            question_text = statement
            model_answer = "True. The statement is supported by the source section."
        else:
            question_text = f"What does the source state about {term} in {topic_tag}?"
            model_answer = sentence

        questions.append(
            {
                "question_text": question_text,
                "question_type": question_type,
                "model_answer": model_answer,
                "rubric": rubric,
                "max_marks": 2,
                "topic_tag": topic_tag,
                "difficulty": "easy",
            }
        )

    return questions


def _stringify_llm_value(value) -> str:
    """Convert occasionally nested LLM fields into readable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " ".join(
            part for item in value if (part := _stringify_llm_value(item))
        ).strip()
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            text = _stringify_llm_value(item)
            if text:
                label = str(key).replace("_", " ").strip().capitalize()
                parts.append(f"{label}: {text}")
        return ". ".join(parts).strip()
    return str(value).strip()


def _validate_questions(questions: list[dict], expected_type: str) -> list[dict]:
    """Filter out incomplete or malformed question dicts."""
    valid = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        if not _REQUIRED_KEYS.issubset(q.keys()):
            continue

        q["question_text"] = _stringify_llm_value(q.get("question_text"))
        q["model_answer"] = _stringify_llm_value(q.get("model_answer"))
        q["rubric"] = _stringify_llm_value(q.get("rubric"))
        q["topic_tag"] = _stringify_llm_value(q.get("topic_tag")) or "Statistics"
        q["difficulty"] = _stringify_llm_value(q.get("difficulty")) or "medium"

        if not q["question_text"]:
            continue
        if not q["model_answer"]:
            continue
        # Normalise question_type
        qt = _stringify_llm_value(q.get("question_type", expected_type)).lower().replace(" ", "_")
        if qt not in {"short_answer", "mcq", "true_false"}:
            qt = expected_type
        q["question_type"] = qt
        # Normalise max_marks
        try:
            q["max_marks"] = float(q["max_marks"])
        except (TypeError, ValueError):
            q["max_marks"] = 5.0
        # Normalise difficulty
        diff = q["difficulty"].lower()
        if diff not in {"easy", "medium", "hard"}:
            diff = "medium"
        q["difficulty"] = diff
        valid.append(q)
    return valid
