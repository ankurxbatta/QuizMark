"""
question_generator.py  —  Deep, chunk-aware question generation.

Pipeline:
  1. Receive a list of TextChunk objects (or use DeepSearch retrieval from MongoDB)
  2. Score and rank chunks by teaching value
  3. Group chunks by topic so questions are spread across all chapters
  4. For each topic group, generate questions with:
       - Bloom's Taxonomy cognitive level distribution
       - 4-level uniqueness enforcement vs existing questions
  5. Post-process: validate JSON, deduplicate, assign final metadata
  6. Return sorted list of question dicts ready for DB insertion

DeepSearch for generation (deep_retrieve_for_generation):
  When generating from the Library, instead of loading all chunks and ranking
  by a static heuristic, we:
    1. Generate exam-focused retrieval queries for the chapter topic
       (e.g. "key formulas in X", "conditions and assumptions for X", "worked examples X")
    2. Embed all queries and search the MongoDB vector store in parallel
    3. Deduplicate and return the highest-value chunks for generation
  This targets exactly the content that makes good exam questions.

Bloom's Taxonomy levels (inspired by Shiksha Copilot's question bank approach):
  L1 Remember  — Recall definitions, facts, formulas directly stated
  L2 Understand — Explain or interpret a concept
  L3 Apply     — Use a formula/method to solve a new scenario
  L4 Analyze   — Compare methods, identify patterns, critique
  L5 Evaluate  — Justify a choice or assess a statistical approach

Uniqueness enforcement (4-level, from Shiksha's question_bank_parts_gen):
  1. No direct repetition of existing questions
  2. No rewording of existing questions
  3. No conceptual overlap (same skill tested)
  4. No computational equivalence (same calculation, different numbers)
"""
from __future__ import annotations

import asyncio
import logging
import json
import re
from typing import Optional

from app.core.config import settings
from app.services.llm_service import generation_service, slm_service
from app.services.pdf_service import TextChunk

logger = logging.getLogger(__name__)


def _safe_exception_message(exc: Exception) -> str:
    message = str(exc)
    message = re.sub(r"([?&]key=)[^&\s']+", r"\1***", message)
    message = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1***", message)
    return message


# ─────────────────────────────────────────────────────────────────────────────
#  DeepSearch retrieval for question generation
# ─────────────────────────────────────────────────────────────────────────────

_RETRIEVAL_QUERY_PROMPT = """\
You are designing retrieval queries to find the best textbook content for writing exam questions.

Chapter/Topic: {topic}

Generate {n} short, specific search queries that will surface the most TESTABLE content:
  - Key definitions and formulas
  - Conditions, assumptions, and when to apply methods
  - Worked examples with numbers
  - Common misconceptions or failure modes
  - Comparisons between related concepts

Output ONLY a JSON array of strings. Example:
["formula for {topic}", "conditions when {topic} applies", "example calculation {topic}"]
"""


async def _generate_retrieval_queries(topic: str, n: int = 4) -> list[str]:
    """Use LLM to generate exam-focused retrieval queries for a chapter topic."""
    prompt = _RETRIEVAL_QUERY_PROMPT.format(topic=topic, n=n)
    try:
        raw = await generation_service.generate(prompt)
        raw = re.sub(r"```(?:json)?", "", raw).strip()
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            queries = [str(q).strip() for q in parsed if q and str(q).strip()]
            if queries:
                return queries[:n]
    except Exception:
        pass
    return [topic]


_CONCEPT_EXTRACTION_PROMPT = """\
You are reading a chapter of a statistics textbook to plan exam coverage.

Chapter topic: {topic}

Source excerpts:
{excerpts}

Identify the chapter's KEY LEARNING CONCEPTS that an exam should cover. Focus on:
  - Named definitions, theorems, and formulas
  - Conditions / assumptions for applying a method
  - Procedures or worked-example patterns
  - Common pitfalls or comparisons between related ideas

Output ONLY a JSON array of short concept labels (3–8 words each), max {n}. Example:
["finite population correction factor", "conditions for normal approximation", "interpreting p-values"]
"""


async def extract_chapter_concepts(
    topic: str,
    chunks: list,
    n: int = 8,
) -> list[str]:
    """
    'Read the chapter carefully' pass: extract the key learning concepts from a
    set of textbook chunks so downstream retrieval / gap-fill can target them.
    """
    if not chunks:
        return []
    excerpts = "\n\n".join(
        c.to_prompt_block()[:1200] for c in chunks[:6]
    )
    prompt = _CONCEPT_EXTRACTION_PROMPT.format(topic=topic, excerpts=excerpts, n=n)
    try:
        raw = await generation_service.generate(prompt)
        raw = re.sub(r"```(?:json)?", "", raw).strip()
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            concepts = [str(c).strip() for c in parsed if c and str(c).strip()]
            return concepts[:n]
    except Exception:
        pass
    return []


async def deep_retrieve_for_generation(
    topic: str,
    book_id: Optional[str] = None,
    k: int = 12,
) -> list[dict]:
    """
    Multi-query vector search to find the best chunks for question generation.

    Generates exam-focused sub-queries for the topic (formulas, conditions,
    examples, comparisons), runs them in parallel against the MongoDB vector
    store, deduplicates, and returns the highest-value chunks.

    Args:
        topic:    Chapter title or topic name.
        book_id:  Restrict to a specific ingested book (or None for all books).
        k:        Total distinct chunks to return.

    Returns:
        List of raw MongoDB chunk dicts ready for _DbChunk construction.
    """
    from app.services.retrieval_router import routed_retrieve

    queries = await _generate_retrieval_queries(topic, n=4)
    embeddings = await asyncio.gather(*[slm_service.embed(q) for q in queries])

    # Intent-routed multi-index retrieval with RRF fusion (MULTI_RAG_DESIGN
    # Phase 3): sub-queries about formulas/charts also hit the specialist
    # indexes, whose top hits pull their source chunks in via cross-links.
    fused = await routed_retrieve(queries, embeddings, book_id=book_id, k=k)
    return fused.text_chunks[:k]


# ─────────────────────────────────────────────────────────────────────────────
#  DbChunk — lightweight TextChunk-compatible wrapper for MongoDB docs
# ─────────────────────────────────────────────────────────────────────────────

class DbChunk:
    """Wraps a raw MongoDB pdf_chunks document as a TextChunk-compatible object."""

    def __init__(self, doc: dict):
        self.chapter_num = doc.get("chapter_num", 0)
        self.chapter_title = doc.get("chapter_title", "Unknown")
        self.section_title = doc.get("section_title", "")
        self.topic_tag = doc.get("topic_tag", self.chapter_title)
        self.text = doc.get("text", "")
        self.page_start = doc.get("page_start", 0)
        self.page_end = doc.get("page_end", 0)
        self.has_formula = doc.get("has_formula", False)
        self.has_example = doc.get("has_example", False)
        self.teaching_density = doc.get("teaching_density", 0.5)
        self.key_terms = doc.get("key_terms", [])
        self.image_texts = doc.get("image_texts", [])
        self.table_texts = doc.get("table_texts", [])
        self.math_text = doc.get("math_text", "")

    @property
    def label(self) -> str:
        return f"Ch{self.chapter_num} § {self.section_title}"

    def to_prompt_block(self) -> str:
        parts = [
            f"[SOURCE: {self.label} | Topic: {self.topic_tag} | "
            f"Pages {self.page_start}–{self.page_end}]",
        ]
        if self.has_formula:
            parts.append("[Contains: mathematical formulas]")
        if self.has_example:
            parts.append("[Contains: worked examples]")
        if self.table_texts:
            parts.append("[Contains: extracted tables]")
        if self.image_texts:
            parts.append("[Contains: extracted image/chart text]")
        parts.append("")
        parts.append(self.text)
        if self.table_texts:
            parts.append("\n[EXTRACTED TABLES]")
            parts.extend(self.table_texts)
        if self.math_text:
            parts.append("\n[FORMULA SNIPPETS]")
            parts.append(self.math_text)
        if self.image_texts:
            parts.append("\n[IMAGE/CHART TEXT]")
            parts.extend(self.image_texts)
        return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
#  Targeted Bloom's level generation
# ─────────────────────────────────────────────────────────────────────────────

# Maps a Bloom's level to the exact instruction injected into the prompt
_BLOOM_LEVEL_INSTRUCTIONS: dict[str, str] = {
    "L1": (
        "BLOOM'S L1 — REMEMBER ONLY: Every question must ask students to RECALL a specific "
        "definition, label, symbol, formula name, or fact directly stated in the source. "
        "No interpretation, no calculation, no comparison."
    ),
    "L2": (
        "BLOOM'S L2 — UNDERSTAND ONLY: Every question must ask students to EXPLAIN, "
        "SUMMARISE, or CLASSIFY a concept. Students should demonstrate they understand "
        "what a term or result means — not just recall it, not yet apply it."
    ),
    "L3": (
        "BLOOM'S L3 — APPLY ONLY: Every question must present a concrete numerical scenario "
        "or dataset and ask students to USE a formula or method to solve it. "
        "Pure recall or explanation is not sufficient — the answer requires computation or procedure."
    ),
    "L4": (
        "BLOOM'S L4 — ANALYZE ONLY: Every question must ask students to COMPARE two methods, "
        "IDENTIFY a violated assumption, BREAK DOWN a multi-part problem, or EXPLAIN WHY "
        "a specific technique is appropriate or inappropriate for a given situation."
    ),
    "L5": (
        "BLOOM'S L5 — EVALUATE ONLY: Every question must ask students to JUSTIFY a statistical "
        "decision, CRITIQUE a flawed approach, ASSESS the validity of a conclusion, or "
        "RECOMMEND a method with reasoned justification. Expect paragraph-length answers."
    ),
}

_TARGETED_BLOOM_PROMPT = """\
You are a statistics assessment author writing exam questions for a business statistics course.

SOURCE CONTENT (base all questions strictly on this material):
{content}

{formulas_block}
COGNITIVE LEVEL REQUIREMENT:
{bloom_instruction}

bloom_level for ALL questions in your output: "{bloom_level}"

Generate EXACTLY {count} questions of type "{qtype}" that satisfy the cognitive level requirement above.

{uniqueness_block}

━━━ SHORT ANSWER ━━━
- Ask about a SPECIFIC concept, formula, condition, or scenario — not vague prompts.
- Model answer: 2–5 precise sentences. Include formulas or numeric results where relevant.
- Rubric: one criterion per mark. Format: "1 mark: <what student must state>."

━━━ MCQ ━━━
- Stem poses a clear question about a concept, condition, formula, or scenario.
- Put stem and options together in question_text:
  Stem text?
  A. First option
  B. Second option
  C. Third option
  D. Fourth option
- Four substantive options; distractors reflect common misconceptions.
- Model answer: correct letter + brief explanation.

━━━ TRUE/FALSE ━━━
- Test application or interpretation, NOT just a copied definition.
- Model answer: "True" or "False" + 1–2 sentence justification.

━━━ ALL TYPES ━━━
- max_marks: L1=2, L2=2, L3=4, L4=6, L5=8
- difficulty: easy (L1–L2) | medium (L3–L4) | hard (L5)
- topic_tag: chapter/concept area
- bloom_level: MUST be "{bloom_level}" for every question

Respond ONLY as a valid JSON array. Required keys per question:
question_text, question_type, model_answer, rubric, max_marks, topic_tag, difficulty, bloom_level
MCQ elements may also include an optional options object with keys A, B, C, D.

No preamble. No trailing text. Just the JSON array.
"""


async def _specialist_context(best_chunk, bloom_level: str, book_id: Optional[str]) -> str:
    """
    Bloom-level → specialist-index routing (MULTI_RAG_DESIGN):
      L3 Apply   → exact formulas from math_index
      L4 Analyze → figures + tables from figure_index / table_index
    Returns a ready-to-insert prompt block ("" when nothing applies/retrieves).
    """
    blocks: list[str] = []
    try:
        if bloom_level == "L3" and settings.MATH_INDEX_ENABLED:
            from app.services.math_index import retrieve_formulas, render_formulas_block
            query = f"{best_chunk.topic_tag} {best_chunk.section_title} formula calculation".strip()
            q_emb = await slm_service.embed(query)
            block = render_formulas_block(await retrieve_formulas(q_emb, book_id=book_id, k=5))
            if block:
                blocks.append(block)

        elif bloom_level == "L4" and (settings.FIGURE_INDEX_ENABLED or settings.TABLE_INDEX_ENABLED):
            query = f"{best_chunk.topic_tag} {best_chunk.section_title} data chart interpretation".strip()
            q_emb = await slm_service.embed(query)
            if settings.FIGURE_INDEX_ENABLED:
                from app.services.figure_index import retrieve_figures, render_figures_block
                block = render_figures_block(await retrieve_figures(q_emb, book_id=book_id, k=3))
                if block:
                    blocks.append(block)
            if settings.TABLE_INDEX_ENABLED:
                from app.services.table_index import retrieve_tables, render_tables_block
                block = render_tables_block(await retrieve_tables(q_emb, book_id=book_id, k=2))
                if block:
                    blocks.append(block)
    except Exception as exc:
        logger.debug(f"[GEN] specialist index retrieval skipped: {_safe_exception_message(exc)}")

    return ("\n\n".join(blocks) + "\n") if blocks else ""


async def generate_targeted_bloom_questions(
    chunks: list,
    question_type: str,
    count: int,
    bloom_level: str,
    existing_questions: Optional[list[str]] = None,
    book_id: Optional[str] = None,
) -> list[dict]:
    """
    Generate `count` questions locked to a single Bloom's level.
    Used by the orchestrator to fill gaps after Round 1.

    Bloom-level → modality routing (MULTI_RAG_DESIGN): L3 (Apply) prompts are
    augmented with exact formulas from the math specialist index, and L4
    (Analyze) prompts with real figures and tables from the figure/table
    indexes — so computational questions are built on verbatim repaired LaTeX
    and analysis questions reference actual charts/tables from the book.
    Degrades silently to chunk-only context if the indexes are empty/disabled.
    """
    if not chunks or count <= 0:
        return []

    bloom_instruction = _BLOOM_LEVEL_INSTRUCTIONS.get(bloom_level, "")
    uniqueness_block = _build_uniqueness_block(existing_questions or [])

    # Pick the best chunk (highest teaching density) as the focal context
    best = max(chunks, key=_score_chunk)
    content = best.to_prompt_block()

    formulas_block = await _specialist_context(best, bloom_level, book_id)

    prompt = _TARGETED_BLOOM_PROMPT.format(
        content=content,
        formulas_block=formulas_block,
        bloom_instruction=bloom_instruction,
        bloom_level=bloom_level,
        count=count,
        qtype=question_type,
        uniqueness_block=uniqueness_block,
    )
    try:
        raw = await generation_service.generate(prompt)
        questions = _parse_json_array(raw)
    except Exception as e:
        logger.warning(f"[GEN] targeted bloom {bloom_level} failed: {_safe_exception_message(e)}")
        questions = []

    # Force correct bloom_level and metadata
    for q in questions:
        q["bloom_level"] = bloom_level
        diff = {"L1": "easy", "L2": "easy", "L3": "medium", "L4": "medium", "L5": "hard"}
        q["difficulty"] = diff.get(bloom_level, "medium")
        if not q.get("topic_tag"):
            q["topic_tag"] = best.topic_tag
        q["_source_chunk"] = best.label
        q["_page_range"] = f"{best.page_start}-{best.page_end}"

    return _validate_questions(questions, question_type)


# ─────────────────────────────────────────────────────────────────────────────
#  Bloom's Taxonomy distribution (Shiksha-inspired)
# ─────────────────────────────────────────────────────────────────────────────

_BLOOMS_GUIDE = """\
BLOOM'S TAXONOMY — distribute questions across these cognitive levels:
  L1 Remember  (easy)   — Recall a definition, fact, formula, or label directly from source
  L2 Understand (easy)  — Explain a concept, summarise what a result means, or classify data
  L3 Apply    (medium)  — Use a formula/method to solve a specific numerical scenario
  L4 Analyze  (medium)  — Compare two methods, identify an assumption, or break down a problem
  L5 Evaluate  (hard)   — Justify a statistical decision, critique an approach, or assess validity

Target distribution for {count} questions:
  ~20% L1 Remember, ~20% L2 Understand, ~30% L3 Apply, ~20% L4 Analyze, ~10% L5 Evaluate

Add a "bloom_level" field (L1–L5) to every question object.
"""

_UNIQUENESS_BLOCK = """\
UNIQUENESS REQUIREMENTS — your questions must NOT:
  1. Directly repeat any existing question (verbatim match)
  2. Be a reworded version of any existing question (same concept, different phrasing)
  3. Conceptually overlap (test the exact same skill or fact as an existing question)
  4. Be computationally equivalent (same calculation with only the numbers changed)

EXISTING QUESTIONS IN THE BANK (avoid overlap with ALL of these):
{existing_questions_block}
"""

# ─────────────────────────────────────────────────────────────────────────────
#  Prompt templates
# ─────────────────────────────────────────────────────────────────────────────

_PLAIN_TEXT_PROMPT = """\
You are a statistics assessment author writing exam questions for a business statistics course.

SOURCE TEXT:
{content}

Generate {count} high-quality exam questions of type "{qtype}".

{blooms_guide}
{uniqueness_block}

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
- max_marks: 2–8 depending on complexity (L1=2, L2=2, L3=4, L4=6, L5=8).
- difficulty: easy (L1–L2) | medium (L3–L4) | hard (L5).
- topic_tag: the chapter or concept area (e.g. "Confidence Intervals", "Normal Distribution").
- bloom_level: "L1" | "L2" | "L3" | "L4" | "L5"

Respond ONLY as a valid JSON array with required keys:
question_text, question_type, model_answer, rubric, max_marks, topic_tag, difficulty, bloom_level
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


_DIFFICULTY_INSTRUCTION = {
    "easy":   "DIFFICULTY REQUIREMENT: ALL questions must be EASY (Bloom's L1–L2) — recall of a single definition, term, or fact directly stated in the source. No calculation or multi-step reasoning.",
    "medium": "DIFFICULTY REQUIREMENT: ALL questions must be MEDIUM (Bloom's L3–L4) — apply a formula, interpret a statistical result, or reason through a scenario. Not pure recall.",
    "hard":   "DIFFICULTY REQUIREMENT: ALL questions must be HARD (Bloom's L5) — require multi-step calculation, comparison of two+ concepts, or critical evaluation of a method or result.",
}


def _build_uniqueness_block(existing_questions: list[str]) -> str:
    """Build the uniqueness instruction block from a list of existing question texts."""
    if not existing_questions:
        return ""
    lines = "\n".join(f"  - {q[:120]}" for q in existing_questions[:40])
    return _UNIQUENESS_BLOCK.format(existing_questions_block=lines)


async def _generate_from_chunk(
    chunk: TextChunk,
    question_type: str,
    questions_per_chunk: int,
    difficulty: str = "all",
    existing_questions: Optional[list[str]] = None,
) -> list[dict]:
    """Single-stage generation with Bloom's Taxonomy distribution and uniqueness enforcement."""
    diff_note = _DIFFICULTY_INSTRUCTION.get(difficulty, "")
    content = chunk.to_prompt_block()
    if diff_note:
        content += f"\n\n{diff_note}"

    blooms_guide = _BLOOMS_GUIDE.format(count=questions_per_chunk)
    uniqueness_block = _build_uniqueness_block(existing_questions or [])

    prompt = _PLAIN_TEXT_PROMPT.format(
        content=content,
        count=questions_per_chunk,
        qtype=question_type,
        blooms_guide=blooms_guide,
        uniqueness_block=uniqueness_block,
    )
    try:
        raw = await generation_service.generate(prompt)
        questions = _parse_json_array(raw)
    except Exception as e:
        logger.warning(f"[GEN] chunk generation failed: {_safe_exception_message(e)}")
        questions = []

    if not questions:
        logger.info("[GEN] using deterministic fallback questions for chunk")
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
    existing_questions: Optional[list[str]] = None,
) -> list[dict]:
    """
    Generate `count` questions from a list of TextChunk objects.

    Applies Bloom's Taxonomy distribution and 4-level uniqueness enforcement
    against any existing questions passed via `existing_questions`.
    Spreads questions across topics unless topic_filter is set.
    difficulty: "easy" | "medium" | "hard" | "all" (LLM distributes across Bloom's)
    """
    logger.info(f"[GEN] generate_questions_from_chunks: {len(chunks)} chunks, qtype={question_type}, count={count}, difficulty={difficulty}")

    if not chunks:
        logger.warning("[GEN] No chunks provided!")
        return []

    # How many chunks to process and questions per chunk
    num_chunks = min(max((count + 2) // 3, 1), len(chunks), 15)
    questions_per_chunk = max(2, (count // num_chunks) + 1)

    logger.info(f"[GEN] Processing {num_chunks} chunks, {questions_per_chunk} questions per chunk")

    selected = _select_chunks(chunks, num_chunks, topic_filter)
    logger.info(f"[GEN] Selected {len(selected)} chunks")

    # Process chunks concurrently (up to 3 at a time)
    all_questions: list[dict] = []
    semaphore = asyncio.Semaphore(3)

    async def _bounded(chunk):
        async with semaphore:
            return await _generate_from_chunk(
                chunk,
                question_type,
                questions_per_chunk,
                difficulty=difficulty,
                existing_questions=existing_questions,
            )

    results = await asyncio.gather(*[_bounded(c) for c in selected])
    for r in results:
        all_questions.extend(r)

    logger.info(f"[GEN] Generated {len(all_questions)} questions from all chunks")

    # Deduplicate by question_text similarity (simple prefix check)
    seen: set[str] = set()
    deduped: list[dict] = []
    for q in all_questions:
        key = q.get("question_text", "")[:60].lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(q)

    logger.info(f"[GEN] After dedup: {len(deduped)} questions")

    # Validate required fields
    valid = _validate_questions(deduped, question_type)
    logger.info(f"[GEN] After validation: {len(valid)} questions")

    return valid[:count]


async def generate_questions(
    content: str,
    question_type: str,
    count: int = 20,
    existing_questions: Optional[list[str]] = None,
) -> list[dict]:
    """
    Direct generation from plain text with Bloom's Taxonomy distribution
    and uniqueness enforcement vs existing_questions.
    """
    logger.info(f"[GEN] Starting direct question generation with question_type={question_type}, count={count}")
    logger.info(f"[GEN] Content length: {len(content)} chars")

    blooms_guide = _BLOOMS_GUIDE.format(count=count)
    uniqueness_block = _build_uniqueness_block(existing_questions or [])

    prompt = _PLAIN_TEXT_PROMPT.format(
        content=content[:4000],
        count=count,
        qtype=question_type,
        blooms_guide=blooms_guide,
        uniqueness_block=uniqueness_block,
    )
    try:
        logger.info("[GEN] Generating questions directly...")
        raw = await generation_service.generate(prompt)
        logger.info(f"[GEN] LLM response length: {len(raw)}")
        all_questions = _parse_json_array(raw)
        logger.info(f"[GEN] Generated {len(all_questions)} questions")
    except Exception as e:
        logger.warning(f"[GEN] Direct generation failed: {_safe_exception_message(e)}")
        all_questions = []

    if not all_questions:
        logger.info("[GEN] using deterministic fallback questions for text input")
        all_questions = _fallback_questions_from_text(
            content,
            question_type,
            count,
            topic_tag="Statistics",
        )

    result = _validate_questions(all_questions, question_type)[:count]
    logger.info(f"[GEN] Valid after validation: {len(result)}")
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
        # Normalise bloom_level (optional field, default based on difficulty)
        bloom = _stringify_llm_value(q.get("bloom_level", "")).upper()
        if bloom not in {"L1", "L2", "L3", "L4", "L5"}:
            bloom = {"easy": "L2", "medium": "L3", "hard": "L5"}.get(diff, "L3")
        q["bloom_level"] = bloom
        valid.append(q)
    return valid
