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
- Focus on definitions, formulas, conditions, interpretations, and applied scenarios.
- Prefer concepts that can be turned into calculation or application questions.
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

Write each skeleton into a complete, high-quality exam question. Study the style rules and examples below carefully.

━━━ SHORT ANSWER ━━━
Style rules:
- Ask about a SPECIFIC concept, formula, condition, or calculation — never vague "explain" or "describe" prompts.
- Where the source contains numbers, formulas, or scenarios, build the question around them.
- Questions may present a scenario or data and ask the student to calculate, identify, or interpret.
- Model answer: 2–5 precise sentences a student could realistically write. Include formulas or numeric results where relevant.
- Rubric: one criterion per mark. Format: "1 mark: <what student must state>. 1 mark: ..."

Good short-answer examples (style to emulate):
  • "Svetlana charges a one-time fee of $25 plus $15 per hour for tutoring. Write the linear equation for her total earnings per session, and identify the independent and dependent variables."
  • "The ages of smartphone users (13–55+) follow a normal distribution with mean 36.9 years and standard deviation 13.9 years. What is the probability that a randomly selected user is at most 50.8 years old?"
  • "Explain the expected value of the F-ratio when the null hypothesis is true, and what causes deviations from this value."
  • "Under what conditions is the Finite Population Correction Factor applied, and what is its purpose?"

BAD short-answer example (do NOT write this style):
  • "Based on the text, explain in your own words what conditional probability means." ← too vague, not grounded in source data

━━━ MCQ ━━━
Style rules:
- The stem must pose a clear, meaningful question about a statistical concept, condition, formula, or scenario.
- Put the stem and options together in question_text using EXACTLY this line format:
  Stem text?
  A. First option
  B. Second option
  C. Third option
  D. Fourth option
- NEVER generate fill-in-the-blank sentences that just blank out a word from the text.
- NEVER use the source text wording directly as an answer option.
- All four options (A–D) must be substantive and plausible; distractors should reflect common misconceptions.
- Only one option is unambiguously correct.
- Model answer: start with the correct letter, then a concise explanation, e.g. "B. Increasing n lowers the standard error, so the interval becomes narrower."
- Never omit the options. Preferred format: put the A-D options inside question_text. If you use a separate options/choices field, it must be an object with keys A, B, C, D.

Good MCQ examples (style to emulate):
  • question_text: "A researcher increases the sample size of a study from 36 to 100 while keeping all other factors constant. What happens to the confidence interval?
A. It becomes wider.
B. It becomes narrower.
C. It remains the same.
D. It becomes less accurate."
  • question_text: "Which of the following is NOT a characteristic of a binomial experiment?
A. There are only two possible outcomes per trial.
B. The probability of success changes with each trial.
C. The number of trials is fixed.
D. Each trial is independent."
  • question_text: "In a one-way ANOVA, what does the null hypothesis state?
A. All group means are equal.
B. All individual observations are identical.
C. The sample variances must all be zero.
D. The data must contain exactly two groups."

BAD MCQ example (do NOT write this style):
  • "Which term best completes the statement? '____ is called the chi-square distribution.'" ← this is a trivial cloze, not a real question.

━━━ TRUE/FALSE ━━━
Style rules:
- The statement must test application or interpretation, NOT just a definition from the text.
- Prefer statements that involve a specific numerical claim, a consequence of a formula, or a practical condition.
- Avoid pure theory statements that any student could guess without understanding.
- Model answer: state "True" or "False" then give a 1–2 sentence justification citing the source concept.

Good T/F examples (style to emulate):
  • "True or False: When constructing a confidence interval for a population mean, if the sample size is 80, it is acceptable to substitute the sample standard deviation (s) for σ without significant bias."
  • "True or False: Increasing the sample size when constructing a confidence interval, while keeping all other factors constant, will result in a wider confidence interval."

━━━ ALL QUESTION TYPES ━━━
Also provide:
- max_marks: integer (2 for simple recall, 4 for standard application, 6 for analysis, 8 for multi-step)
- topic_tag: chapter/topic (e.g. "Normal Distribution", "Confidence Intervals")
- difficulty: easy | medium | hard
  easy   = recall a definition or read a formula directly
  medium = apply a formula, interpret a result, or reason through a scenario
  hard   = multi-step calculation or critical comparison of concepts

Respond ONLY as a valid JSON array. Each element must have these required keys:
question_text, question_type, model_answer, rubric, max_marks, topic_tag, difficulty
MCQ elements may also include an optional options or choices object with keys A, B, C, D.

No preamble. No trailing text. Just the JSON array.
"""

# Fallback for plain-text (no chunks)
_PLAIN_TEXT_PROMPT = """\
You are a statistics assessment author writing exam questions for a business statistics course.

SOURCE TEXT:
{content}

Generate {count} high-quality exam questions of type "{qtype}".

Follow the style rules and examples below carefully.

━━━ SHORT ANSWER ━━━
- Ask about a SPECIFIC concept, formula, condition, or calculation — not vague "explain" or "describe" prompts.
- Where the source contains numbers, formulas, or scenarios, build the question around them.
- Model answer: 2–5 precise sentences. Include formulas or numeric results where relevant.
- Rubric: one criterion per mark. Format: "1 mark: <what student must state>. 1 mark: ..."

Good examples:
  • "Svetlana charges a one-time fee of $25 plus $15 per hour. Write the linear equation for her total earnings and identify the independent and dependent variables."
  • "The ages of smartphone users follow a normal distribution with mean 36.9 and SD 13.9. What is P(X ≤ 50.8)?"
  • "Under what conditions is the Finite Population Correction Factor applied, and what is its purpose?"

BAD example (avoid): "Based on the text, explain in your own words what conditional probability means."

━━━ MCQ ━━━
- Stem must pose a clear, meaningful question about a concept, condition, formula, or scenario.
- Put the stem and options together in question_text using EXACTLY this line format:
  Stem text?
  A. First option
  B. Second option
  C. Third option
  D. Fourth option
- NEVER generate fill-in-the-blank sentences that just blank out a word from the text.
- Four substantive options (A–D); distractors should reflect common misconceptions.
- Only one option is unambiguously correct.
- Model answer: start with the correct letter + brief explanation, e.g. "B. Increasing n lowers the standard error."
- Never omit the options. Preferred format: put the A-D options inside question_text. If you use a separate options/choices field, it must be an object with keys A, B, C, D.

Good examples:
  • question_text: "A researcher increases sample size from 36 to 100, all else equal. What happens to the confidence interval?
A. It becomes wider.
B. It becomes narrower.
C. It stays exactly the same.
D. It becomes less connected to the sample."
  • question_text: "Which is NOT a characteristic of a binomial experiment?
A. There are only two possible outcomes per trial.
B. The probability of success changes from trial to trial.
C. The number of trials is fixed.
D. Trials are independent."

BAD example (avoid): "Which term best completes: '____ is the chi-square distribution'?" ← trivial cloze.

━━━ TRUE/FALSE ━━━
- Test application or interpretation, NOT just a copied definition.
- Prefer statements involving a specific numerical consequence, formula condition, or practical rule.
- Model answer: "True" or "False" + 1–2 sentence justification referencing the source concept.

Good examples:
  • "True or False: When n = 80, substituting the sample SD for σ in a confidence interval formula introduces significant bias."
  • "True or False: Increasing sample size while holding all else constant produces a wider confidence interval."

━━━ ALL TYPES ━━━
- Spread questions across different concepts in the source.
- max_marks: 2–8 depending on complexity.
- difficulty: easy (recall) | medium (apply/interpret) | hard (multi-step/compare).
- topic_tag: the chapter or concept area (e.g. "Confidence Intervals", "Normal Distribution").

Respond ONLY as a valid JSON array with required keys:
question_text, question_type, model_answer, rubric, max_marks, topic_tag, difficulty
MCQ elements may also include an optional options or choices object with keys A, B, C, D.
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


_DIFFICULTY_INSTRUCTION = {
    "easy":   "DIFFICULTY REQUIREMENT: ALL questions must be EASY — recall of a single definition, term, or fact directly stated in the source. No calculation or multi-step reasoning.",
    "medium": "DIFFICULTY REQUIREMENT: ALL questions must be MEDIUM — apply a formula, interpret a statistical result, or reason through a scenario. Not pure recall.",
    "hard":   "DIFFICULTY REQUIREMENT: ALL questions must be HARD — require multi-step calculation, comparison of two+ concepts, or critical evaluation of a method or result.",
}


async def _generate_from_chunk(
    chunk: TextChunk,
    question_type: str,
    questions_per_chunk: int,
    difficulty: str = "all",
) -> list[dict]:
    """Robust single-stage generation. Direct prompt per chunk, no fragile Stage A dependency."""
    diff_note = _DIFFICULTY_INSTRUCTION.get(difficulty, "")
    extra = f"\n\n{diff_note}" if diff_note else ""
    prompt = _PLAIN_TEXT_PROMPT.format(
        content=chunk.to_prompt_block() + extra,
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

    # Enforce requested difficulty on all generated questions
    if difficulty in ("easy", "medium", "hard"):
        for q in questions:
            q["difficulty"] = difficulty

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
    difficulty: str = "all",
) -> list[dict]:
    """
    Generate `count` questions from a list of TextChunk objects.
    Uses two-stage SLM+LLM pipeline per chunk.
    Spreads questions across topics unless topic_filter is set.
    difficulty: "easy" | "medium" | "hard" | "all" (LLM decides)
    """
    print(f"[GEN] generate_questions_from_chunks: {len(chunks)} chunks, qtype={question_type}, count={count}, difficulty={difficulty}")
    
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
            return await _generate_from_chunk(chunk, question_type, questions_per_chunk, difficulty=difficulty)

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
            # Build a genuine comprehension MCQ — never a trivial cloze
            wrong = _distractors(term, terms)
            while len(wrong) < 3:
                wrong.append("none of the above")
            question_text = (
                f"Which of the following best describes {term} in the context of {topic_tag}?\n"
                f"A. {sentence}\n"
                f"B. {term.capitalize()} only applies when every data value is identical.\n"
                f"C. {term.capitalize()} is unrelated to probability or sampling.\n"
                f"D. {term.capitalize()} eliminates the need to interpret data in context."
            )
            model_answer = f"A. The source directly states: {sentence}"
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


_MCQ_LETTERS = ("A", "B", "C", "D")
_MCQ_OPTION_MARKER = re.compile(r"^\s*(?:option\s*)?([A-D])[\).:\-]\s+", re.IGNORECASE | re.MULTILINE)


def _clean_option_text(value) -> str:
    text = _normalise_text(_stringify_llm_value(value))
    text = text.strip(" \t\r\n-:;")
    return text


def _split_mcq_text(value) -> tuple[str, dict[str, str]]:
    """
    Split an MCQ string into stem and A-D options.
    Handles options on separate lines, with a fallback for "Options: A..." text.
    """
    text = _stringify_llm_value(value)
    if not text:
        return "", {}

    scan_offset = 0
    scan_text = text
    matches = list(_MCQ_OPTION_MARKER.finditer(scan_text))
    option_label = None
    if not matches:
        option_label = re.search(r"\b(?:options|choices|answers)\s*[:\-]\s*", text, re.IGNORECASE)
        if option_label:
            scan_offset = option_label.end()
            scan_text = text[scan_offset:]
            inline_pattern = re.compile(r"(?<![A-Za-z0-9])(?:option\s*)?([A-D])[\).:\-]\s+", re.IGNORECASE)
            matches = list(inline_pattern.finditer(scan_text))
    if not matches:
        return _normalise_text(text), {}

    stem_end = option_label.start() if option_label else scan_offset + matches[0].start()
    stem = text[:stem_end]
    stem = re.sub(r"(?:options|choices|answers)\s*[:\-]?\s*$", "", stem, flags=re.IGNORECASE)
    stem = _normalise_text(stem)

    options: dict[str, str] = {}
    for i, match in enumerate(matches):
        letter = match.group(1).upper()
        start = scan_offset + match.end()
        end = scan_offset + matches[i + 1].start() if i + 1 < len(matches) else len(text)
        option_text = _clean_option_text(text[start:end])
        if option_text and letter in _MCQ_LETTERS:
            options[letter] = option_text
    return stem, options


def _options_from_raw(value) -> dict[str, str]:
    """Accept common LLM shapes such as options dicts/lists/strings."""
    if value is None:
        return {}

    if isinstance(value, dict):
        options: dict[str, str] = {}
        for key, item in value.items():
            key_text = str(key).strip().upper()
            match = re.search(r"\b([A-D])\b", key_text)
            if key_text in _MCQ_LETTERS:
                letter = key_text
            elif match:
                letter = match.group(1)
            elif key_text[-1:] in _MCQ_LETTERS:
                letter = key_text[-1]
            else:
                continue
            text = _clean_option_text(item)
            if text:
                options[letter] = text
        return options

    if isinstance(value, list):
        options: dict[str, str] = {}
        for index, item in enumerate(value[:4]):
            fallback_letter = _MCQ_LETTERS[index]
            if isinstance(item, dict):
                raw_letter = item.get("letter") or item.get("label") or fallback_letter
                letter_match = re.search(r"[A-D]", str(raw_letter).upper())
                letter = letter_match.group(0) if letter_match else fallback_letter
                text = (
                    item.get("text")
                    or item.get("option")
                    or item.get("answer")
                    or item.get("value")
                    or item.get("content")
                )
                if text is None:
                    text = {
                        k: v
                        for k, v in item.items()
                        if k not in {"letter", "label", "is_correct", "correct"}
                    }
            else:
                letter = fallback_letter
                text = item
            option_text = _clean_option_text(text)
            if option_text:
                options[letter] = option_text
        return options

    _, options = _split_mcq_text(value)
    return options


def _correct_letter_from_answer(model_answer: str, options: dict[str, str]) -> str | None:
    patterns = (
        r"^\s*([A-D])[\).:\-]?\b",
        r"(?:the\s+)?(?:correct\s+)?(?:answer|option|choice)(?:\s+is)?\s*[:\-]?\s*([A-D])\b",
        r"\boption\s+([A-D])\b",
        r"\bchoice\s+([A-D])\b",
    )
    for pattern in patterns:
        match = re.search(pattern, model_answer, re.IGNORECASE)
        if match:
            return match.group(1).upper()

    answer_lower = model_answer.lower()
    for letter, text in options.items():
        if text and text.lower() in answer_lower:
            return letter
    return None


def _clean_correct_option_from_answer(model_answer: str) -> str:
    text = _normalise_text(model_answer)
    text = re.sub(
        r"^\s*(?:the\s+)?(?:correct\s+)?(?:answer|option|choice)(?:\s+is)?\s*[:\-]?\s*[A-D][\).:\-]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^\s*[A-D][\).:\-]\s*", "", text, flags=re.IGNORECASE)
    text = text.strip()
    if len(text) > 180:
        sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip()
        text = sentence if 20 <= len(sentence) <= 180 else text[:180].rstrip(" ,;:") + "."
    return text or "The best answer correctly applies the concept in the question."


def _generic_mcq_distractors(topic_tag: str, correct_text: str) -> list[str]:
    topic = topic_tag or "this topic"
    candidates = [
        f"It applies only when every observed value in {topic} is identical.",
        "It removes the need to check the conditions or assumptions of the method.",
        "It reverses the interpretation of the relationship described in the question.",
        "It treats a sample result as if it were always the exact population value.",
        "It is unrelated to probability, sampling, or statistical inference.",
    ]
    correct_key = correct_text.lower()
    return [c for c in candidates if c.lower() != correct_key][:3]


def _normalise_mcq(q: dict, raw_question_text) -> None:
    """Ensure MCQs are stored as stem + A-D options inside question_text."""
    stem, embedded_options = _split_mcq_text(raw_question_text)

    structured_options: dict[str, str] = {}
    if isinstance(raw_question_text, dict):
        stem = (
            _stringify_llm_value(raw_question_text.get("stem"))
            or _stringify_llm_value(raw_question_text.get("question"))
            or stem
        )
        for field in ("options", "choices", "answer_options", "answers"):
            structured_options.update(_options_from_raw(raw_question_text.get(field)))

    for field in ("options", "choices", "answer_options", "answers"):
        structured_options.update(_options_from_raw(q.get(field)))

    options = embedded_options.copy()
    if len(structured_options) >= len(options):
        options.update(structured_options)
    else:
        structured_options.update(options)
        options = structured_options

    stem = stem or _normalise_text(_stringify_llm_value(raw_question_text))
    stem = re.sub(r"\s*(?:options|choices|answers)\s*[:\-]?\s*$", "", stem, flags=re.IGNORECASE).strip()
    if not stem:
        stem = "Which option best answers the question?"

    model_answer = _stringify_llm_value(q.get("model_answer"))
    correct_letter = _correct_letter_from_answer(model_answer, options) or "A"
    if correct_letter not in _MCQ_LETTERS:
        correct_letter = "A"

    correct_text = options.get(correct_letter) or _clean_correct_option_from_answer(model_answer)
    if not options:
        options[correct_letter] = correct_text
    elif correct_letter not in options:
        options[correct_letter] = correct_text

    distractors = iter(_generic_mcq_distractors(q.get("topic_tag", "Statistics"), correct_text))
    for letter in _MCQ_LETTERS:
        if not options.get(letter):
            options[letter] = next(distractors, f"An incorrect interpretation of {q.get('topic_tag', 'the concept')}.")

    options = {letter: _clean_option_text(options[letter]) for letter in _MCQ_LETTERS}
    q["question_text"] = "\n".join(
        [stem, *(f"{letter}. {options[letter]}" for letter in _MCQ_LETTERS)]
    )

    if not _correct_letter_from_answer(model_answer, options):
        q["model_answer"] = f"{correct_letter}. {correct_text}"
    elif not re.match(r"^\s*[A-D][\).:\-]?", model_answer, re.IGNORECASE):
        q["model_answer"] = f"{correct_letter}. {model_answer}"
    else:
        q["model_answer"] = model_answer

    q["rubric"] = q.get("rubric") or "Full marks: selects the correct option."


def _validate_questions(questions: list[dict], expected_type: str) -> list[dict]:
    """Filter out incomplete or malformed question dicts."""
    valid = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        if not _REQUIRED_KEYS.issubset(q.keys()):
            continue

        raw_question_text = q.get("question_text")
        q["question_text"] = _stringify_llm_value(raw_question_text)
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
        if expected_type in {"short_answer", "mcq", "true_false"}:
            qt = expected_type
        q["question_type"] = qt
        if q["question_type"] == "mcq":
            _normalise_mcq(q, raw_question_text)
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
