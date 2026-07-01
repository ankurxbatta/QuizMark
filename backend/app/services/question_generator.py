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
    chapter_num: Optional[int] = None,
    k: int = 12,
    return_context: bool = False,
):
    """
    Multi-query vector search to find the best chunks for question generation.

    Generates exam-focused sub-queries for the topic (formulas, conditions,
    examples, comparisons), runs them in parallel against the MongoDB vector
    store, deduplicates, and returns the highest-value chunks.

    Args:
        topic:    Chapter title or topic name.
        book_id:  Restrict to a specific ingested book (or None for all books).
        k:        Total distinct chunks to return.
        return_context: when True also return the FusedContext so callers can
                  inject the specialist (formula/figure/table) index blocks into
                  the generation prompt — otherwise that retrieved content is
                  computed but never used.

    Returns:
        List of raw MongoDB chunk dicts ready for _DbChunk construction, or
        ``(chunks, FusedContext)`` when ``return_context`` is set.
    """
    from app.services.retrieval_router import routed_retrieve

    queries = await _generate_retrieval_queries(topic, n=4)
    embeddings = await asyncio.gather(*[slm_service.embed(q) for q in queries])

    # Intent-routed multi-index retrieval with RRF fusion (MULTI_RAG_DESIGN
    # Phase 3): sub-queries about formulas/charts also hit the specialist
    # indexes, whose top hits pull their source chunks in via cross-links.
    fused = await routed_retrieve(queries, embeddings, book_id=book_id, chapter_num=chapter_num, k=k)
    chunks = fused.text_chunks[:k]
    if return_context:
        return chunks, fused
    return chunks


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
        "BLOOM'S L5 — EVALUATE ONLY: Every question must require genuine higher-order work and "
        "must NOT be answerable by a single recalled fact or one isolated computation. Each "
        "question must do at least one of: (a) chain TWO or more dependent computation steps; "
        "(b) COMBINE two or more distinct concepts or formulas; or (c) JUSTIFY a statistical "
        "decision, CRITIQUE a flawed approach, ASSESS the validity of a conclusion, or RECOMMEND "
        "a method with reasoned justification. Never restate a recall or single-step apply "
        "question. Expect multi-step reasoning and paragraph-length answers."
    ),
}

_TARGETED_BLOOM_PROMPT = """\
You are an expert statistics instructor writing exam questions that assess genuine understanding of the chapter's CORE concepts, methods, and interpretation — aligned to what a student is expected to learn in a business statistics course.

SOURCE CONTENT (base all questions strictly on this material):
{content}

Base every question STRICTLY on the SOURCE CONTENT and SEED EXERCISES provided. Do NOT use facts, examples, or terminology from other chapters or outside knowledge.

{seed_exercises_block}
{formulas_block}
COGNITIVE LEVEL REQUIREMENT:
{bloom_instruction}

bloom_level for ALL questions in your output: "{bloom_level}"

Generate EXACTLY {count} questions of type "{qtype}" that satisfy the cognitive level requirement above.

{uniqueness_block}

━━━ RELEVANCE — assess genuine understanding ━━━
- Every question MUST test a NAMED statistical concept or skill present in the SOURCE CONTENT (e.g. relative/cumulative frequency, a probability rule, a distribution's use, a sampling-distribution idea, a hypothesis test, interpreting a confidence interval, ANOVA reasoning) — anchor the question to that concept.
- FORBIDDEN — dataset trivia / pure lookup: never ask the student to merely read a single value from a table or recite an incidental detail of an example's narrative (names, places, one-off dataset counts, e.g. "how many towns received between 9.01 and 11.03 inches?") UNLESS that lookup is a step inside a genuine computation or interpretation. This reinforces the trivial-recall ban.
- When a table or figure is used, the question must require a SKILL — compute, interpret, compare, draw a conclusion, or justify — NOT "find cell X".
- Prefer TRANSFERABLE questions about the method/concept over questions bound to one specific dataset's quirks.
- Stay on THIS chapter's concepts; do not drift to unrelated topics.

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
- Every distractor must be factually FALSE as an answer to the stem — never a
  rephrasing or equivalent formulation of the correct option.
- Model answer: correct letter + brief explanation.
- Rubric: the student only SELECTS an option — there is NO place to write an
  interpretation or justification. Use a SINGLE all-or-nothing criterion, e.g.
  "Full marks for selecting the correct option." NEVER split it into "correct
  calculation" / "correct interpretation" marks.

━━━ TRUE/FALSE ━━━
- Test application or interpretation, NOT just a copied definition.
- Model answer: "True" or "False" + 1–2 sentence justification.
- Rubric: the student only SELECTS True or False — use a SINGLE criterion
  ("Full marks for the correct selection"). NEVER ask for written interpretation.

━━━ ALL TYPES ━━━
- SELF-CONTAINED: every question must be fully answerable from ONLY what it shows. NEVER reference a table, figure, graph, chart, plot or "data below/above" unless you ALSO include it — either inline in the question text (a markdown table) OR as a structured asset (see TABLES & FIGURES below). If you cannot include it, do not refer to it — ask a different question.
- NEVER cite a source label such as "Table 1.9", "Figure 2.3", "Example 1.15", or any "Table/Figure/Example/Exercise <number>". The student cannot see the book. Reproduce any needed data inline and refer to it generically (e.g. "the table below", "the following data", "the figure below").
- If you reproduce a data table in a question, every cell must hold its correct value. Only ever leave a cell blank if THIS question explicitly asks the student to compute that exact value — and then the stem must name it. Never emit a silently incomplete table.
- NO PLACEHOLDERS: never leave "____", "[blank]", "<...>", "TODO", or a bare "?" standing in for a value, unless the question explicitly asks the student to compute that exact missing cell. Finish every sentence; never end mid-thought.

━━━ TABLES & FIGURES (decide per question) ━━━
- Decide PER QUESTION whether a table or figure genuinely helps test the concept. MOST questions need NONE — add an asset only when it is essential to the skill being tested, never as decoration.
- TABLE: when a table helps, CONSTRUCT a clean table from the ACTUAL numbers in the source material and attach it as the question's table asset. Fill EVERY cell with its correct value; leave a cell blank ONLY when THIS question explicitly asks the student to compute that exact cell. Refer to it in the stem generically as "the table below".
- FIGURE: when a chart/graph helps (e.g. a histogram or scatter to interpret), do NOT assume an image already exists. Write a concise figure SPEC — chart type, axes with labels/units, the values/series to plot, and what it shows — and attach it as the question's figure asset. Refer to it in the stem generically as "the figure below".
- Attach via an OPTIONAL "assets" array on the question object (at most ONE asset per question):
    table:  {{"kind": "table", "caption": "<short caption, no source label>", "table_markdown": "| Class | Frequency |\\n| 0-10 | 4 |\\n| 10-20 | 9 |"}}
    figure: {{"kind": "figure", "caption": "<short caption>", "figure_spec": "A right-skewed unimodal distribution curve; x-axis = value, y-axis = frequency; long tail to the right. Qualitative SHAPE only — no specific numbers."}}
  A question that includes a complete table/figure asset and refers to "the table/figure below" is fully self-contained. Never reference an asset you do not include, and never cite a source label like "Table 1.9" / "Figure 2.3".
- Write EVERY mathematical expression as inline LaTeX wrapped in single dollar signs, e.g. $P(x)=\\frac{{\\mu^x e^{{-\\mu}}}}{{x!}}$, $\\bar{{x}}$, $\\sigma^2$, $\\binom{{n}}{{k}}p^k(1-p)^{{n-k}}$. Use \\mu, \\sigma, \\lambda for Greek letters. This applies to the stem, every option, and the model answer.
- max_marks: L1=2, L2=2, L3=4, L4=6, L5=8
- difficulty: easy (L1–L2) | medium (L3–L4) | hard (L5)
- topic_tag: chapter/concept area
- bloom_level: MUST be "{bloom_level}" for every question

Respond ONLY as a valid JSON array. Required keys per question:
question_text, question_type, model_answer, rubric, max_marks, topic_tag, difficulty, bloom_level
MCQ elements may also include an optional options object with keys A, B, C, D.
Any question MAY also include an optional "assets" array (one table or figure) as described under TABLES & FIGURES.

No preamble. No trailing text. Just the JSON array.
"""


async def _specialist_context(best_chunk, bloom_level: str, book_id: Optional[str], chapter_num: Optional[int] = None) -> str:
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
            block = render_formulas_block(await retrieve_formulas(q_emb, book_id=book_id, chapter_num=chapter_num, k=5))
            if block:
                blocks.append(block)

        elif bloom_level == "L4" and (settings.FIGURE_INDEX_ENABLED or settings.TABLE_INDEX_ENABLED):
            query = f"{best_chunk.topic_tag} {best_chunk.section_title} data chart interpretation".strip()
            q_emb = await slm_service.embed(query)
            if settings.FIGURE_INDEX_ENABLED:
                from app.services.figure_index import retrieve_figures, render_figures_block
                block = render_figures_block(await retrieve_figures(q_emb, book_id=book_id, chapter_num=chapter_num, k=3))
                if block:
                    blocks.append(block)
            if settings.TABLE_INDEX_ENABLED:
                from app.services.table_index import retrieve_tables, render_tables_block
                block = render_tables_block(await retrieve_tables(q_emb, book_id=book_id, chapter_num=chapter_num, k=2))
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
    chapter_num: Optional[int] = None,
    seed_exercises: Optional[list[dict]] = None,
    asset_directive: str = "",
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
    if asset_directive:
        content += f"\n\n{asset_directive}"

    formulas_block = await _specialist_context(best, bloom_level, book_id, chapter_num)
    seed_exercises_block = _render_seed_exercises_block(seed_exercises)

    prompt = _TARGETED_BLOOM_PROMPT.format(
        content=content,
        seed_exercises_block=seed_exercises_block,
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
    _generic_topics = ("Unknown", "Statistics", "", "chapter/concept area", "chapter or concept area")
    for q in questions:
        q["bloom_level"] = bloom_level
        diff = {"L1": "easy", "L2": "easy", "L3": "medium", "L4": "medium", "L5": "hard"}
        q["difficulty"] = diff.get(bloom_level, "medium")
        if not q.get("topic_tag") or q["topic_tag"] in _generic_topics:
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

# ─────────────────────────────────────────────────────────────────────────────
#  Auto-rejection criteria — SINGLE SOURCE OF TRUTH.
#  These mirror, in plain language, exactly what the post-generation quality gate
#  (answer_verifier._passes_renderability + _judge_question) DROPS. Injected into
#  the generation prompt so the model self-censors against the same checklist that
#  would otherwise discard its output. When you tighten the gate, update this block
#  too (and vice-versa) so generation and validation never drift apart.
#  NOTE: this is prompt-level steering for a frozen hosted model — there is no
#  training/reward; the model simply sees the reject rules up front.
# ─────────────────────────────────────────────────────────────────────────────
_REJECTION_CRITERIA = """\
━━━ AUTO-REJECTION CHECK — a question matching ANY rule below is DISCARDED before the student ever sees it ━━━
Producing a rejected question WASTES the slot and the request comes back short, so DO NOT emit one. Reject if the question:
1. Refers to a table, figure, graph, chart, dataset, or "data below/above" WITHOUT attaching it. This is NOT a ban on data/figure questions — the fix is to ATTACH what you reference: an inline markdown table, or a `figure_spec` / table entry in the question's `assets` array. A figure question that carries a `figure_spec` asset is CORRECT and passes the gate (its image is drawn later). Only a DANGLING reference — one with nothing attached — is rejected.
2. Cites a book source label — "Table 1.9", "Figure 2.3", "Example 1.15", "Exercise 4", or any "Table/Figure/Example/Exercise <number>".
3. Contains a placeholder — "____", "[blank]", "<...>", "TODO", or a bare "?" standing in for a value — UNLESS the stem explicitly asks the student to compute that exact missing cell.
4. Has unbalanced math delimiters: every "$" must be paired, and every "{", "(", "[" must be closed.
5. Is truncated or ends mid-sentence.
6. (MCQ) has fewer than 4 options, has duplicate/equivalent options, or names a correct answer that is not one of the listed options.
7. Cannot be answered using ONLY the question text plus its own attached asset (not self-contained).
8. Has a model answer whose numeric result is wrong for the data the question gives.

SELF-CHECK: before you output, re-read every question against rules 1–8 and FIX or REPLACE any that would be rejected. Output ONLY questions that pass all eight."""


_PLAIN_TEXT_PROMPT = """\
You are an expert statistics instructor writing exam questions that assess genuine understanding of the chapter's CORE concepts, methods, and interpretation — aligned to what a student is expected to learn in a business statistics course.

SOURCE TEXT:
{content}

Base every question STRICTLY on the SOURCE CONTENT and SEED EXERCISES provided. Do NOT use facts, examples, or terminology from other chapters or outside knowledge.

{seed_exercises_block}
Generate {count} high-quality exam questions of type "{qtype}".

{blooms_guide}
{uniqueness_block}

{rejection_criteria}

Follow the style rules and examples below carefully.

━━━ RELEVANCE — assess genuine understanding ━━━
- Every question MUST test a NAMED statistical concept or skill present in the SOURCE TEXT (e.g. relative/cumulative frequency, a probability rule, a distribution's use, a sampling-distribution idea, a hypothesis test, interpreting a confidence interval, ANOVA reasoning) — anchor the question to that concept.
- FORBIDDEN — dataset trivia / pure lookup: never ask the student to merely read a single value from a table or recite an incidental detail of an example's narrative (names, places, one-off dataset counts, e.g. "how many towns received between 9.01 and 11.03 inches?") UNLESS that lookup is a step inside a genuine computation or interpretation. This reinforces the trivial-recall ban.
- When a table or figure is used, the question must require a SKILL — compute, interpret, compare, draw a conclusion, or justify — NOT "find cell X".
- Prefer TRANSFERABLE questions about the method/concept over questions bound to one specific dataset's quirks.
- Stay on THIS chapter's concepts; do not drift to unrelated topics.

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
- Every distractor must be factually FALSE as an answer to the stem — never a
  rephrasing, special case, or equivalent formulation of the correct option
  (e.g. if the correct option is "P(X ≤ 5)", a distractor must not be "the
  area under the pdf up to 5" — that is the same statement in other words).
- Model answer: start with the correct letter + brief explanation, e.g. "B. Increasing n lowers the standard error."
- Rubric: the student only SELECTS an option — there is NO place to write an interpretation or justification. Use a SINGLE all-or-nothing criterion, e.g. "Full marks for selecting the correct option." NEVER split it into "correct calculation" / "correct interpretation" marks.
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
- Rubric: the student only SELECTS True or False — use a SINGLE criterion ("Full marks for the correct selection"). NEVER ask for written interpretation.

Good examples:
  • "True or False: When n = 80, substituting the sample SD for σ in a confidence interval formula introduces significant bias."
  • "True or False: Increasing sample size while holding all else constant produces a wider confidence interval."

━━━ ALL TYPES ━━━
- SELF-CONTAINED: every question must be fully answerable from ONLY what it shows. NEVER reference a table, figure, graph, chart, plot or "data below/above" unless you ALSO include it — either inline in the question text (a markdown table) OR as a structured asset (see TABLES & FIGURES below). If you cannot include it, do not refer to it — ask a different question.
- NEVER cite a source label such as "Table 1.9", "Figure 2.3", "Example 1.15", or any "Table/Figure/Example/Exercise <number>". The student cannot see the book. Reproduce any needed data inline and refer to it generically (e.g. "the table below", "the following data", "the figure below").
- If you reproduce a data table in a question, every cell must hold its correct value. Only ever leave a cell blank if THIS question explicitly asks the student to compute that exact value — and then the stem must name it. Never emit a silently incomplete table.
- NO PLACEHOLDERS: never leave "____", "[blank]", "<...>", "TODO", or a bare "?" standing in for a value, unless the question explicitly asks the student to compute that exact missing cell. Finish every sentence; never end mid-thought.

━━━ TABLES & FIGURES (decide per question) ━━━
- Decide PER QUESTION whether a table or figure genuinely helps test the concept. MOST questions need NONE — add an asset only when it is essential to the skill being tested, never as decoration.
- TABLE: when a table helps, CONSTRUCT a clean table from the ACTUAL numbers in the source material and attach it as the question's table asset. Fill EVERY cell with its correct value; leave a cell blank ONLY when THIS question explicitly asks the student to compute that exact cell. Refer to it in the stem generically as "the table below".
- FIGURE: when a chart/graph helps (e.g. a histogram or scatter to interpret), do NOT assume an image already exists. Write a concise figure SPEC — chart type, axes with labels/units, the values/series to plot, and what it shows — and attach it as the question's figure asset. Refer to it in the stem generically as "the figure below".
- Attach via an OPTIONAL "assets" array on the question object (at most ONE asset per question):
    table:  {{"kind": "table", "caption": "<short caption, no source label>", "table_markdown": "| Class | Frequency |\\n| 0-10 | 4 |\\n| 10-20 | 9 |"}}
    figure: {{"kind": "figure", "caption": "<short caption>", "figure_spec": "A right-skewed unimodal distribution curve; x-axis = value, y-axis = frequency; long tail to the right. Qualitative SHAPE only — no specific numbers."}}
  A question that includes a complete table/figure asset and refers to "the table/figure below" is fully self-contained. Never reference an asset you do not include, and never cite a source label like "Table 1.9" / "Figure 2.3".
- Write EVERY mathematical expression as inline LaTeX wrapped in single dollar signs, e.g. $P(x)=\\frac{{\\mu^x e^{{-\\mu}}}}{{x!}}$, $\\bar{{x}}$, $\\sigma^2$, $\\binom{{n}}{{k}}p^k(1-p)^{{n-k}}$. Use \\mu, \\sigma, \\lambda for Greek letters. This applies to the stem, every option, and the model answer.
- Spread questions across different concepts in the source.
- max_marks: 2–8 depending on complexity (L1=2, L2=2, L3=4, L4=6, L5=8).
- difficulty: easy (L1–L2) | medium (L3–L4) | hard (L5).
- topic_tag: the chapter or concept area (e.g. "Confidence Intervals", "Normal Distribution").
- bloom_level: "L1" | "L2" | "L3" | "L4" | "L5"

Respond ONLY as a valid JSON array with required keys:
question_text, question_type, model_answer, rubric, max_marks, topic_tag, difficulty, bloom_level
MCQ elements may also include an optional options or choices object with keys A, B, C, D.
Any question MAY also include an optional "assets" array (one table or figure) as described under TABLES & FIGURES.
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
    "easy": (
        "DIFFICULTY REQUIREMENT — EASY (Bloom's L1–L2). Each question must test a SINGLE "
        "recalled fact, definition, symbol, or formula NAME directly stated in the source, "
        "answerable in exactly ONE step. STRICTLY FORBIDDEN: any calculation, any "
        "interpretation of a result, any applied scenario, any multi-part reasoning. "
        "The answer is a single recalled item, nothing more.\n"
        "  GOOD (easy): \"State the formula for the mean of a binomial distribution.\"\n"
        "  BAD  (too hard for easy): \"A factory has p=0.2 over 50 trials — compute the expected number of defects.\""
    ),
    "medium": (
        "DIFFICULTY REQUIREMENT — MEDIUM (Bloom's L3–L4). Each question must require APPLYING "
        "ONE formula or method to a given scenario/dataset, OR INTERPRETING a single given "
        "result — exactly ONE main computation or one act of interpretation. It must NOT be "
        "answerable by pure recall of a fact (that would be easy), and it must NOT require "
        "chaining several computations or combining multiple distinct concepts (that would be hard).\n"
        "  GOOD (medium): \"Given mean=4 for a Poisson process, compute P(X=2).\"\n"
        "  BAD  (too easy): \"What does lambda represent in the Poisson distribution?\""
    ),
    "hard": (
        "DIFFICULTY REQUIREMENT — HARD (Bloom's L5). Each question MUST demand genuine "
        "higher-order work and MUST NOT be answerable without multi-step reasoning. It must "
        "do AT LEAST ONE of: (a) require TWO or more chained computation steps where a later "
        "step depends on an earlier result; (b) COMBINE two or more distinct concepts/formulas; "
        "or (c) EVALUATE, JUSTIFY, or CRITIQUE a method, assumption, or conclusion with reasoned "
        "argument. STRICTLY FORBIDDEN: any question answerable by a single recalled fact, a "
        "one-line lookup, or a single isolated computation; and you must NOT restate, reword, "
        "or trivially extend an easy or medium question. If a question can be answered correctly "
        "in one step, it is NOT acceptable as hard.\n"
        "  GOOD (hard): \"A sample of 60 has mean 4.1 defects; first compute the Poisson "
        "probability of 0 defects, then evaluate whether the Poisson model is justified for this "
        "process and explain why.\"\n"
        "  BAD  (recall masquerading as hard): \"True/False: the area under a pdf equals 1.\""
    ),
}


def _render_seed_exercises_block(seed_exercises: Optional[list[dict]]) -> str:
    """Render real chapter exercises as a prompt block ("" when none)."""
    if not seed_exercises:
        return ""
    try:
        from app.services.exercise_index import render_exercises_block
        block = render_exercises_block(seed_exercises)
    except Exception:
        return ""
    return (block + "\n") if block else ""


def _is_trivial_recall(question_text: str, bloom_level: str = "") -> bool:
    """Conservative heuristic: True only when a question is clearly pure recall.

    Used to drop/down-rank items emitted under difficulty="hard" that collapse to
    a one-line lookup. Deliberately strict (all conditions must hold) so genuine
    hard questions are never discarded:
      - very short stem (few words), AND
      - no digit anywhere (no numeric scenario/computation), AND
      - no scenario/applied-reasoning cue verbs, AND
      - bloom_level would be L1 (or unset/recall).
    """
    text = (question_text or "").strip()
    if not text:
        return True  # an empty hard question is never useful
    words = re.findall(r"[A-Za-z]+", text)
    if len(words) > 14:
        return False  # long enough to plausibly carry real reasoning
    if any(ch.isdigit() for ch in text):
        return False  # has numbers → likely a computation/scenario
    lowered = text.lower()
    scenario_cues = (
        "compute", "calculate", "evaluate", "justify", "compare", "explain why",
        "derive", "assess", "critique", "recommend", "determine", "given", "suppose",
        "if ", "estimate", "analyse", "analyze", "show that", "prove", "find the",
    )
    if any(cue in lowered for cue in scenario_cues):
        return False
    bloom = str(bloom_level or "").upper()
    if bloom and bloom != "L1":
        return False
    return True


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
    seed_exercises: Optional[list[dict]] = None,
    asset_directive: str = "",
    extra_context: str = "",
) -> list[dict]:
    """Single-stage generation with Bloom's Taxonomy distribution and uniqueness enforcement."""
    diff_note = _DIFFICULTY_INSTRUCTION.get(difficulty, "")
    content = chunk.to_prompt_block()
    if extra_context:
        # Specialist-index context (repaired formulas, real figures/tables for
        # this chapter) so mainline generation uses the dedicated indexes too,
        # not just the chunk prose.
        content += f"\n\n{extra_context}"
    if diff_note:
        content += f"\n\n{diff_note}"
    if asset_directive:
        content += f"\n\n{asset_directive}"

    blooms_guide = _BLOOMS_GUIDE.format(count=questions_per_chunk)
    uniqueness_block = _build_uniqueness_block(existing_questions or [])
    seed_exercises_block = _render_seed_exercises_block(seed_exercises)

    prompt = _PLAIN_TEXT_PROMPT.format(
        content=content,
        seed_exercises_block=seed_exercises_block,
        count=questions_per_chunk,
        qtype=question_type,
        blooms_guide=blooms_guide,
        uniqueness_block=uniqueness_block,
        rejection_criteria=_REJECTION_CRITERIA,
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
            difficulty=difficulty,
        )

    # Enforce requested difficulty on all generated questions — and keep
    # bloom_level inside that difficulty's band so we never emit an internally
    # inconsistent item (e.g. difficulty=hard with bloom_level=L1).
    if difficulty in ("easy", "medium", "hard"):
        band = {"easy": {"L1", "L2"}, "medium": {"L3", "L4"}, "hard": {"L5"}}[difficulty]
        default_bloom = {"easy": "L2", "medium": "L3", "hard": "L5"}[difficulty]
        for q in questions:
            q["difficulty"] = difficulty
            if str(q.get("bloom_level", "")).upper() not in band:
                q["bloom_level"] = default_bloom

        # Conservative guard: for HARD output, drop items that are clearly
        # trivial recall (a short, number-free, scenario-free stem). Only drop
        # when at least one genuine hard question survives, so we never empty
        # the result set just because the heuristic flagged everything.
        if difficulty == "hard":
            kept = [q for q in questions if not _is_trivial_recall(q.get("question_text", ""), q.get("bloom_level", ""))]
            if kept and len(kept) < len(questions):
                logger.info(f"[GEN] hard guard dropped {len(questions) - len(kept)} trivial-recall question(s)")
                questions = kept

    # Stamp correct metadata from the chunk
    _generic_topics = ("Unknown", "Statistics", "", "chapter/concept area", "chapter or concept area")
    for q in questions:
        if not q.get("topic_tag") or q["topic_tag"] in _generic_topics:
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
    seed_exercises: Optional[list[dict]] = None,
    asset_directive: str = "",
    extra_context: str = "",
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
                seed_exercises=seed_exercises,
                asset_directive=asset_directive,
                extra_context=extra_context,
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
        seed_exercises_block="",
        count=count,
        qtype=question_type,
        blooms_guide=blooms_guide,
        uniqueness_block=uniqueness_block,
        rejection_criteria=_REJECTION_CRITERIA,
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

    # Quality passes: recompute numeric model answers, de-ambiguate MCQ options
    from app.services.answer_verifier import verify_generated_questions
    result = await verify_generated_questions(result)
    # Render any figure-spec image only AFTER the gate (and drop a figure question
    # whose image can't be produced, so the stem never dangles a missing figure).
    from app.services.question_assets import realize_figure_images
    return await realize_figure_images(result)


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def unmangle_latex(obj):
    """Repair LaTeX commands corrupted by JSON parsing.

    LaTeX like ``\\binom`` / ``\\frac`` starts with ``\\b`` / ``\\f``, which are
    JSON escape sequences (backspace / form-feed). When the model emits them
    single-escaped, json.loads turns ``\\b``→ a backspace char, leaving ``inom``.
    Backspace (\\x08) and form-feed (\\x0c) never occur legitimately in question
    text, so converting them back to ``\\b`` / ``\\f`` restores the command.
    """
    if isinstance(obj, str):
        return obj.replace("\x08", "\\b").replace("\x0c", "\\f")
    if isinstance(obj, list):
        return [unmangle_latex(x) for x in obj]
    if isinstance(obj, dict):
        return {k: unmangle_latex(v) for k, v in obj.items()}
    return obj


def _parse_json_array(raw: str) -> list[dict]:
    """Extract and parse the first JSON array from raw LLM output."""
    # Strip markdown code fences if present
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    start = raw.find("[")
    if start == -1:
        return []
    # Decode the first well-formed JSON value starting at '['. raw_decode stops
    # at the end of that value, so any trailing prose or a second bracketed block
    # (e.g. an appended answer key) can't corrupt/truncate the question array the
    # way a greedy ``\[.*\]`` match would.
    try:
        parsed, _ = json.JSONDecoder().raw_decode(raw[start:])
        if isinstance(parsed, list):
            return unmangle_latex(parsed)
    except json.JSONDecodeError:
        pass
    # Fallback: greedy span + partial recovery for malformed/truncated output.
    match = re.search(r"\[.*\]", raw[start:], re.DOTALL)
    if not match:
        return []
    try:
        return unmangle_latex(json.loads(match.group()))
    except json.JSONDecodeError:
        # Try to recover partial JSON
        try:
            partial = match.group().rstrip(",] \n") + "]"
            return unmangle_latex(json.loads(partial))
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
    # No last-resort append of raw text: a junk chunk (e.g. a stray page number
    # like "80.") must yield NO candidate so the fallback skips it rather than
    # fabricating a meaningless question.
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
    difficulty: str = "easy",
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
                "difficulty": difficulty if difficulty in ("easy", "medium", "hard") else "easy",
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
# Inline-capable marker: matches "A." / "A)" anywhere as long as the letter is not
# part of a word/number (so "grade A." inside prose only matches when it actually
# starts an A→B→C run). Used to find the real options even when the model emits
# them inline on the stem line.
_MCQ_INLINE_MARKER = re.compile(r"(?<![A-Za-z0-9])(?:option\s+)?([A-D])[\).:\-]\s+", re.IGNORECASE)


def _clean_option_text(value) -> str:
    text = _normalise_text(_stringify_llm_value(value))
    text = text.strip(" \t\r\n-:;")
    return text


def _first_option_run(markers: list[tuple[str, int, int]]) -> list[tuple[str, int, int]] | None:
    """Return the first contiguous A→B→C(→D) marker run.

    The model sometimes appends a SECOND option block to ``question_text`` — an
    answer key, or the model answer re-listed with the correct letter annotated
    ("B. … is the correct expression"). Both start by repeating "A"/"B", which
    breaks the strictly-increasing sequence, so taking only the first ordered run
    keeps the genuine options and discards the leaked answer block. Requiring at
    least A, B and C in order guards against a stray "A." in prose.
    """
    for i, (letter, _s, _e) in enumerate(markers):
        if letter != "A":
            continue
        run = [markers[i]]
        for nxt in markers[i + 1:]:
            if len(run) < 4 and nxt[0] == _MCQ_LETTERS[len(run)]:
                run.append(nxt)
            else:
                break
        if len(run) >= 3:
            return run
    return None


def _split_mcq_text(value) -> tuple[str, dict[str, str]]:
    """
    Split an MCQ string into stem and A-D options.
    Prefers the first inline A→B→C(→D) run (so options the model wrote inline on
    the stem line are found and any trailing answer-key block is ignored), with a
    fallback for "Options: A..." text.
    """
    text = _stringify_llm_value(value)
    if not text:
        return "", {}

    markers = [(m.group(1).upper(), m.start(), m.end()) for m in _MCQ_INLINE_MARKER.finditer(text)]
    run = _first_option_run(markers)
    if run:
        stem = re.sub(r"\s*(?:options|choices|answers)\s*[:\-]?\s*$", "", text[:run[0][1]], flags=re.IGNORECASE)
        stem = _normalise_text(stem)
        # The genuine options end where the marker after the run begins (the start
        # of any trailing duplicate/answer block), or at end of text.
        last_idx = markers.index(run[-1])
        run_end = markers[last_idx + 1][1] if last_idx + 1 < len(markers) else len(text)
        options: dict[str, str] = {}
        for idx, (letter, _s, mend) in enumerate(run):
            end = run[idx + 1][1] if idx + 1 < len(run) else run_end
            options[letter] = _clean_option_text(text[mend:end])
        return stem, options

    # Fallback: "Options: A) …" label form (no clean inline run found).
    option_label = re.search(r"\b(?:options|choices|answers)\s*[:\-]\s*", text, re.IGNORECASE)
    matches: list = []
    scan_offset = 0
    if option_label:
        scan_offset = option_label.end()
        matches = list(_MCQ_INLINE_MARKER.finditer(text[scan_offset:]))
    if not matches:
        matches = list(_MCQ_OPTION_MARKER.finditer(text))
        scan_offset = 0
    if not matches:
        return _normalise_text(text), {}

    stem_end = option_label.start() if option_label else scan_offset + matches[0].start()
    stem = text[:stem_end]
    stem = re.sub(r"(?:options|choices|answers)\s*[:\-]?\s*$", "", stem, flags=re.IGNORECASE)
    stem = _normalise_text(stem)

    options = {}
    for i, match in enumerate(matches):
        letter = match.group(1).upper()
        start = scan_offset + match.end()
        end = scan_offset + matches[i + 1].start() if i + 1 < len(matches) else len(text)
        option_text = _clean_option_text(text[start:end])
        if option_text and letter in _MCQ_LETTERS and letter not in options:
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
    filled_letters: list[str] = []
    for letter in _MCQ_LETTERS:
        if not options.get(letter):
            options[letter] = next(distractors, f"An incorrect interpretation of {q.get('topic_tag', 'the concept')}.")
            filled_letters.append(letter)
    if filled_letters:
        # Generic placeholders keep the question well-formed, but they read as
        # boilerplate; the verification pass replaces them with topic-specific
        # false distractors written by the LLM.
        q["_generic_distractors"] = filled_letters

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

    # Structured answer key — marking compares this directly instead of
    # re-parsing the model answer prose (no LLM call, no regex fragility).
    q["correct_answer"] = correct_letter

    q["rubric"] = q.get("rubric") or "Full marks: selects the correct option."


def _derive_true_false_key(model_answer: str) -> str:
    """Always return a usable True/False answer key (title-case).

    Marking compares this structured key directly (rag_pipeline fast path); a
    true_false question with no key is routed to manual review instead of being
    auto-marked. Prefer an explicit "true"/"false" token in the model answer,
    then fall back to affirmation/negation cues, and finally default to "True".
    """
    text = (model_answer or "")
    tf = re.search(r"\b(true|false)\b", text, re.IGNORECASE)
    if tf:
        return tf.group(1).title()
    # No literal true/false — infer from the prose's stance.
    if re.search(r"\b(incorrect|not\s+correct|false\b|wrong|does\s+not|doesn't|isn't|"
                 r"is\s+not|no,)\b", text, re.IGNORECASE):
        return "False"
    if re.search(r"\b(correct|yes|right|holds|valid|accurate|supported)\b", text, re.IGNORECASE):
        return "True"
    # Nothing to go on — default to a definite key so marking stays deterministic.
    return "True"


# A student-facing question must never cite a book-internal label ("Table 1.9",
# "Figure 2.3", "Example 1.15", "Exercise 3.4") — the student can't see the book,
# and it leaks the source. The data should be inline and referred to generically.
# The real label survives only in DB metadata (source_chunk / page range / book_id /
# topic_tag), which this sanitiser never touches.
#   <num> = 12 | 1.9 | 2.3.1 | 1.9a  (must be preceded by a label word, so plain
#   decimals like "p = 1.9" or "9.01 inches" are left untouched).
# Words that are ALSO ordinary imperatives ("Plot 3 points", "Graph 2 functions",
# "Chart 5 values") only denote a book reference when a CUE word precedes them or
# the number is a decimal ("Chart 3.1"). A bare "Plot 3" is left alone.
_CUE = (r"in|on|see|from|per|of|using|use|with|refer(?:ring)?\s+to|according\s+to"
        r"|based\s+on|shown\s+in|given\s+in|listed\s+in|the|a|an")

_SOURCE_LABEL_RE = re.compile(
    # 1) Unambiguous book-label words — match with any number.
    r"(?:\b(?:the|a|an)\s+)?"
    r"\b(?P<label>tables?|figures?|figs?\.?|exhibits?|examples?|exercises?|problems?)\s+"
    r"\d+(?:\.\d+)*[A-Za-z]?\b"
    r"|"
    # 2) Ambiguous plotting words preceded by a CUE word (the cue is re-emitted).
    r"\b(?P<cue>" + _CUE + r")\s+"
    r"(?P<clabel>graphs?|charts?|diagrams?|histograms?|plots?)\s+"
    r"\d+(?:\.\d+)*[A-Za-z]?\b"
    r"|"
    # 3) Ambiguous plotting words carrying a DECIMAL number (e.g. "Chart 3.1").
    r"\b(?P<dlabel>graphs?|charts?|diagrams?|histograms?|plots?)\s+"
    r"\d+\.\d+(?:\.\d+)*[A-Za-z]?\b",
    re.IGNORECASE,
)


def _strip_source_labels(text: str) -> str:
    """Genericise book-internal cross-references in STUDENT-FACING text.

    "According to Table 1.9, what …" → "According to the table below, what …"
    "Figure 6.1 shows …"             → "The figure below shows …"
    "use the data in Example 1.15"   → "use the data in a worked example"
    A normal sentence (no labels) and bare decimals are returned unchanged.

    Pure function (unit-testable). Apply ONLY to question_text / options / model
    answer — never to metadata fields that preserve provenance.
    """
    if not text:
        return text

    def _repl(match: re.Match) -> str:
        # The label may come from any of the three alternation branches.
        label = (match.group("label") or match.group("clabel") or match.group("dlabel")).lower()
        if label.startswith("table"):
            out = "the table below"
        elif label.startswith((
            "fig", "graph", "chart", "diagram", "histogram", "plot", "exhibit",
        )):
            out = "the figure below"
        elif label.startswith("example"):
            out = "a worked example"
        else:  # exercise / problem
            out = "the following"
        # Capitalise when the reference starts a sentence so grammar still reads.
        prefix = match.string[: match.start()]
        if not prefix.strip() or re.search(r"[.!?:]\s*$", prefix):
            out = out[0].upper() + out[1:]
        # A cue word ("in", "see", …) that qualified an ambiguous plotting word is
        # re-emitted so "In Graph 2, …" → "In the figure below, …" keeps its lead-in.
        cue = match.group("cue")
        if cue:
            out = f"{cue} {out}"
        return out

    cleaned = _SOURCE_LABEL_RE.sub(_repl, text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def _parse_single_json_obj(raw: str) -> Optional[dict]:
    """Parse a single JSON object from an LLM reply (tolerant), unmangle LaTeX."""
    try:
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if not m:
            return None
        obj = json.loads(m.group())
        return unmangle_latex(obj) if isinstance(obj, dict) else None
    except Exception:
        return None


async def generate_table_grounded_questions(
    book_id: Optional[str],
    chapter_num: Optional[int],
    question_type: str,
    count: int,
    difficulty: str = "all",
    existing_questions: Optional[list[str]] = None,
) -> list[dict]:
    """Generate questions grounded in REAL, cleaned data tables from the chapter.

    Asking the model to invent a complete, correct table inline is unreliable —
    it references tables it never includes, so the quality gate (correctly) drops
    them. Instead we hand the model an ACTUAL cleaned table from
    ``pdf_chunks.table_texts`` (the vision-repaired source) and ask for ONE
    question that exercises a statistical skill on it; the SAME table is attached
    to the question, so it is guaranteed self-contained and the asset-aware gate
    passes. Returns gate-verified questions (len <= count); [] to fall back.
    """
    from app.services.mongo_vector_store import _get_db
    from app.services.answer_verifier import verify_generated_questions

    try:
        db = await _get_db()
    except Exception:
        db = None
    if db is None:
        return []

    query: dict = {"table_texts.0": {"$exists": True}}
    if book_id:
        query["book_id"] = book_id
    if chapter_num is not None:
        query["chapter_num"] = chapter_num
    chunks = await db["pdf_chunks"].find(
        query, {"table_texts": 1, "topic_tag": 1, "section_title": 1, "chapter_title": 1},
    ).to_list(length=80)

    # Distinct, well-formed tables only: >=2 columns, >=2 data rows, no '?' gaps,
    # AND a real header row (skip raw data grids whose "headers" are just numbers —
    # they produce weird column names like '0.50 | 4.25 | 5').
    def _is_header_row(row: str) -> bool:
        cells = [c.strip() for c in row.strip("|").split("|") if c.strip()]
        if len(cells) < 2:
            return False
        numeric = sum(1 for c in cells if re.fullmatch(r"[-+]?\$?\d[\d,]*\.?\d*%?", c or ""))
        return numeric <= len(cells) / 2  # majority of header cells must be text labels

    seen: set[str] = set()
    tables: list[tuple[str, str]] = []
    for c in chunks:
        topic = c.get("topic_tag") or c.get("chapter_title") or ""
        for t in (c.get("table_texts") or []):
            md = (t or "").strip()
            if not md or "?" in md or md.count("|") < 4:
                continue
            data_rows = [r for r in md.splitlines() if r.strip() and (set(r) - {"-", ":", "|", " "})]
            if len(data_rows) < 3:
                continue
            if not _is_header_row(data_rows[0]):
                continue
            key = md[:80]
            if key in seen:
                continue
            seen.add(key)
            tables.append((md, topic))
    if not tables:
        logger.info("[TABLE-GEN] no clean tables for chapter — falling back to standard generation")
        return []

    qtype = question_type if question_type in {"mcq", "true_false", "short_answer"} else "short_answer"
    if qtype == "mcq":
        type_rules = ('Provide an "options" object with keys A, B, C, D (each distinct) and "correct_answer" '
                      "as the single correct letter; distractors must be plausible but FALSE.")
    elif qtype == "true_false":
        type_rules = 'Make a True/False statement; set "correct_answer" to "True" or "False".'
    else:
        type_rules = 'Provide a full worked "model_answer".'
    diff = difficulty if difficulty in ("easy", "medium", "hard") else "medium"

    sem = asyncio.Semaphore(3)

    async def _one(md: str, topic: str) -> Optional[dict]:
        prompt = (
            f"You are an expert statistics instructor. Here is a real data table from the chapter on {topic}:\n\n"
            f"{md}\n\n"
            f"Write ONE {qtype} exam question (difficulty: {diff}) that requires the student to apply a genuine "
            "statistical SKILL to THIS table — compute, interpret, compare, or draw a conclusion (NEVER a trivial "
            'single-cell lookup). The student SEES this exact table directly below the question, so refer to it as '
            '"the table below" and do NOT reproduce the table in your text. Do NOT cite any source label or table number.\n'
            f"{type_rules}\n"
            "Respond ONLY as a JSON object with keys: question_text, model_answer, rubric, max_marks, topic_tag, difficulty"
            + (", options, correct_answer" if qtype in {"mcq", "true_false"} else "")
            + "."
        )
        async with sem:
            try:
                raw = await generation_service.generate(prompt)
            except Exception as exc:
                logger.warning(f"[TABLE-GEN] generation failed: {_safe_exception_message(exc)}")
                return None
        obj = _parse_single_json_obj(raw)
        if not obj or not obj.get("question_text"):
            return None
        obj["question_type"] = qtype
        obj.setdefault("rubric", "Award marks for correct use of the table.")
        obj.setdefault("max_marks", 3.0)
        obj.setdefault("topic_tag", topic or "Statistics")
        obj.setdefault("difficulty", diff)
        obj["assets"] = [{"kind": "table", "caption": "", "table_markdown": md}]
        return obj

    # A couple extra to survive gate attrition, bounded.
    targets = tables[: count + 2]
    results = await asyncio.gather(*[_one(md, topic) for md, topic in targets])
    raw_qs = [r for r in results if r]
    logger.info(f"[TABLE-GEN] built {len(raw_qs)} table-grounded candidates from {len(targets)} real tables")
    if not raw_qs:
        return []
    valid = _validate_questions(raw_qs, qtype)
    verified = await verify_generated_questions(valid)
    logger.info(f"[TABLE-GEN] {len(verified)} survived the quality gate")
    return verified[:count]


async def generate_figure_grounded_questions(
    book_id: Optional[str],
    chapter_num: Optional[int],
    chapter_topic: str,
    question_type: str,
    count: int,
    difficulty: str = "all",
    existing_questions: Optional[list[str]] = None,
) -> list[dict]:
    """Generate CONCEPTUAL-figure questions directly (focused path, like the
    table path). Each question tests what a SHAPE/PATTERN MEANS — skewness, mean
    vs median, correlation direction/strength, spread/outliers — never reading a
    precise value off the chart, because AI-generated images are reliable only for
    qualitative shapes. The illustration image is generated ONLY after a question
    passes the quality gate. Returns gate-verified, image-bearing questions.
    """
    from app.services.answer_verifier import verify_generated_questions
    from app.services.question_assets import realize_figure_images

    qtype = question_type if question_type in {"mcq", "true_false", "short_answer"} else "short_answer"
    diff = difficulty if difficulty in ("easy", "medium", "hard") else "medium"
    if qtype == "mcq":
        type_rules = ('Provide an "options" object (keys A,B,C,D, distinct) and "correct_answer" as the correct '
                      "letter; distractors plausible but FALSE.")
    elif qtype == "true_false":
        type_rules = 'Make a True/False statement; set "correct_answer" to "True" or "False".'
    else:
        type_rules = 'Provide a full worked "model_answer".'

    sem = asyncio.Semaphore(3)

    async def _one(_i: int) -> Optional[dict]:
        prompt = (
            f"You are an expert statistics instructor writing an exam question for the chapter on {chapter_topic}.\n"
            "Write ONE question built around a CONCEPTUAL figure that illustrates a SHAPE or PATTERN — choose the one "
            "most relevant to this chapter: a right-/left-skewed or symmetric distribution, a normal bell curve, a "
            "positive / negative / no-correlation scatter plot, or a boxplot showing spread and outliers. "
            "The student SEES this figure, so the question MUST test what the shape or pattern MEANS (e.g. skewness and "
            "how the mean and median compare, the direction/strength of a correlation, spread and outliers) — NEVER "
            'reading a precise numeric value off the figure. Refer to it as "the figure below"; do not cite any source label.\n'
            f"{type_rules}\n"
            "Get the statistics RIGHT: for a RIGHT-skewed (positive) distribution mean > median; for a LEFT-skewed "
            "(negative) distribution mean < median; for a symmetric distribution mean ≈ median. A positive correlation "
            "is an upward-trending scatter, negative is downward, near-zero shows no trend. A boxplot's box spans Q1–Q3 "
            "(the IQR) with the median inside; points beyond the whiskers are outliers.\n"
            'Also provide a "figure_spec": a concise description of the qualitative shape to draw (NO specific numbers).\n'
            f"Difficulty: {diff}.\n"
            "Respond ONLY as a JSON object with keys: question_text, model_answer, rubric, max_marks, topic_tag, "
            "difficulty, figure_spec" + (", options, correct_answer" if qtype in {"mcq", "true_false"} else "") + "."
        )
        async with sem:
            try:
                raw = await generation_service.generate(prompt)
            except Exception as exc:
                logger.warning(f"[FIGURE-GEN] generation failed: {_safe_exception_message(exc)}")
                return None
        obj = _parse_single_json_obj(raw)
        if not obj or not obj.get("question_text") or not obj.get("figure_spec"):
            return None
        obj["question_type"] = qtype
        obj.setdefault("rubric", "Award marks for correct interpretation of the figure.")
        obj.setdefault("max_marks", 3.0)
        obj.setdefault("topic_tag", chapter_topic or "Statistics")
        obj.setdefault("difficulty", diff)
        obj["assets"] = [{"kind": "figure", "caption": "", "figure_spec": obj.pop("figure_spec")}]
        return obj

    # Generate extra candidates so enough survive the gate; images are only
    # produced for the (few) that pass, so this adds cheap judge calls, not images.
    results = await asyncio.gather(*[_one(i) for i in range(count + 3)])
    raw_qs = [r for r in results if r]
    logger.info(f"[FIGURE-GEN] built {len(raw_qs)} conceptual-figure candidate(s)")
    if not raw_qs:
        return []
    valid = _validate_questions(raw_qs, qtype)
    verified = await verify_generated_questions(valid)
    realized = await realize_figure_images(verified, chapter_num=chapter_num, book_id=book_id)
    logger.info(f"[FIGURE-GEN] {len(realized)} survived gate + got an image")
    return realized[:count]


def _normalise_assets(q: dict) -> None:
    """Convert a model-emitted ``assets`` array into the stored asset schema.

    The generator prompts let the model attach (at most one) table or figure:
      • table  → ``table_markdown`` is rendered to deterministic HTML (kind 'table',
                 ``table_html`` set) so the existing frontend renders it as-is.
      • figure → a text ``figure_spec`` is preserved (kind 'figure', no image yet)
                 in ``_figure_spec``; the expensive image is generated later, only
                 after the question passes the quality gate.
    Pure/defensive: an unusable asset is dropped (the question is kept; the gate
    then catches a now-missing reference). Caps at one asset to match the rest of
    the pipeline (gate helpers + post-gate figure realization read one asset)."""
    raw = q.get("assets")
    if not isinstance(raw, list) or not raw:
        q.pop("assets", None)
        return
    from app.services.question_assets import render_table_html
    normalised: list[dict] = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        kind = str(a.get("kind", "")).strip().lower()
        markdown = _stringify_llm_value(
            a.get("table_markdown") or a.get("markdown") or a.get("table")
        )
        spec = _stringify_llm_value(
            a.get("figure_spec") or a.get("description") or a.get("spec")
        )
        # The caption is rendered to the student verbatim by the frontend, so it
        # must be genericised too — otherwise a "Table 1.9: …" caption leaks the
        # book label even though question_text was already stripped.
        caption = _strip_source_labels(_stringify_llm_value(a.get("caption")))
        if kind == "table" or (kind != "figure" and markdown):
            html = _stringify_llm_value(a.get("table_html"))
            if not html and markdown:
                html, _ = render_table_html(markdown)
            if "<table" not in (html or "").lower():
                continue  # unparseable table → drop asset
            normalised.append({
                "kind": "table",
                "caption": caption,
                "alt_text": caption or "Data table",
                "table_html": html,
                "image_id": None,
                "source_page": None,
            })
        elif kind == "figure" or spec:
            if not spec:
                continue
            normalised.append({
                "kind": "figure",
                "caption": caption,
                "alt_text": caption or spec[:200],
                "table_html": None,
                "image_id": None,
                "source_page": None,
                "_figure_spec": spec[:1200],
            })
        if normalised:
            break  # one asset per question
    if normalised:
        q["assets"] = normalised[:1]
    else:
        q.pop("assets", None)


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
        # A statement the model phrased as "True or False: …" is far better served
        # as a selectable true_false question (student just picks True/False and it
        # is marked deterministically) than as a free-text box — reclassify it
        # regardless of the requested type.
        if qt != "mcq" and re.match(r"\s*true\s*(?:or|/)\s*false\b", q["question_text"], re.IGNORECASE):
            qt = "true_false"
        q["question_type"] = qt
        if q["question_type"] == "mcq":
            _normalise_mcq(q, raw_question_text)
        elif q["question_type"] == "true_false":
            q["correct_answer"] = _derive_true_false_key(q["model_answer"])
        # Select-only types: the student PICKS an option and never writes prose, so
        # there is nowhere to "interpret the result" or justify an answer. Any
        # multi-criterion rubric the LLM emitted (e.g. "1 mark: correct calculation;
        # 1 mark: correct interpretation") is meaningless here — override it with a
        # single all-or-nothing select criterion. Marking is deterministic on the key.
        if q["question_type"] in {"mcq", "true_false"}:
            q["rubric"] = "Full marks for selecting the correct option; no marks otherwise."
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
        # Strip book-internal source labels from every student-facing field (MCQ
        # options live inside question_text). Runs BEFORE the quality gate so a
        # "Table 1.9" reference becomes "the table below" and is then correctly
        # required to carry an attached table. Metadata fields keep the real label.
        q["question_text"] = _strip_source_labels(q["question_text"])
        q["model_answer"] = _strip_source_labels(q["model_answer"])
        # Normalise any model-emitted table/figure asset into the stored schema
        # (table → HTML now; figure → spec only, image generated post-gate).
        _normalise_assets(q)
        valid.append(q)
    return valid
