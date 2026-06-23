# Question Generation Pipeline

This document explains how the system goes from a raw PDF to a diverse, high-quality question bank.

---

## Step 1 — Book Ingestion

Before questions can be generated, the PDF must be ingested. Ingestion extracts and stores all teachable content in MongoDB with vector embeddings.

**What gets extracted per page:**
- Full text (PyMuPDF markdown extraction)
- Tables (structured markdown rows)
- Math fonts detected (STIX, CMMI, Symbol, MathJax) → pages sent for LaTeX extraction
- Vector graphics detected → pages sent for chart description
- Raster images detected → flagged

**Per chunk stored in MongoDB:**
```
text            — cleaned chapter/section text
math_text       — LaTeX formulas extracted by OpenAI vision
image_texts     — natural-language chart descriptions from OpenAI vision
table_texts     — extracted table rows
embedding       — 768-dim vector (Gemini or OpenAI)
chapter_num     — detected chapter number
chapter_title   — detected chapter title
section_title   — detected section heading
has_formula     — boolean
has_example     — boolean
teaching_density— fraction of lines containing teaching signals
key_terms       — extracted statistical/domain terms
```

Chunks pass through the LangChain ingestion chain before storage:
clean → recursive/semantic chunking → LLM validation (math repair + dedup) → vision → batched embedding.

Ingestion is **resumable** — re-uploading the same PDF continues from the last saved checkpoint.
Failed windows are rolled back and retried on resume, so the store never contains partial output.

---

## Step 2 — DeepSearch Retrieval

When generation is triggered, the system does not simply search for the chapter title. It decomposes the topic using the LLM and runs multiple parallel vector searches.

**Round 0 — Concept extraction:**
```
Input:  "Chapter 3: Probability Topics"
LLM →   ["sample space", "conditional probability", "Bayes theorem",
          "independence", "complement rule", "multiplication rule"]
Output: enriched topic string + concept list
```

**Round 1 — Multi-query retrieval:**
```
4 sub-queries run in parallel against MongoDB vector search:
  Query 1: chapter topic + "definition formula"
  Query 2: chapter topic + "worked example calculation"
  Query 3: chapter topic + "compare analyse"
  Query 4: chapter topic + enriched concept terms

Results: deduplicated, ranked by teaching_density, top-K returned
```

This is inspired by multi-query RAG patterns — a single embedding query misses content phrased differently than the chapter title.

---

## Step 3 — Multi-Round Generation

**Round 1 — Broad generation (~70% of target count):**

The LLM receives the retrieved chunks and generates questions across all Bloom's taxonomy levels:

| Level | Code | Focus |
|---|---|---|
| Remember | L1 | Recall facts, definitions, formulas |
| Understand | L2 | Explain, interpret, summarise |
| Apply | L3 | Calculate, use formula in scenario |
| Analyse | L4 | Compare methods, break down assumptions |
| Evaluate | L5 | Critique, justify, assess |

**Round 2 — Coverage gap fill:**

After Round 1, the system audits the Bloom's distribution. For each under-represented level:
1. Generate a level-specific retrieval query (e.g. for L3: "calculate apply formula worked example numerical")
2. Run targeted vector search
3. Generate more questions locked to that Bloom's level
4. Use Round 1 output as uniqueness context (prevents duplicates)

**Round 3 — Validation and dedup:**
- Remove near-duplicate questions (embedding cosine similarity ≥ 0.92)
- Enforce Bloom's distribution (trim over-represented levels)
- If still below target: run a small top-up pass
- Return final set capped at requested count

**Round 4 — Quality verification (`answer_verifier.py`):**

*Numeric answers* — short-answer model answers containing calculations are
checked by having the LLM extract the final calculation as a pure-Python
expression, which is then evaluated deterministically (restricted namespace).
Inline generation occasionally produces arithmetic errors (e.g. stating
P(X &lt; 5) ≈ 0.265 when the true value is ≈ 0.285); since the marker measures
students against the model answer, those errors would silently penalise
correct answers. Wrong values are rewritten before storage with the
deterministically computed number.

*MCQ distractors* — when the generating LLM omits options, the structural
fallback fills the gaps with generic placeholders; the verification pass
replaces those with topic-specific false distractors written by the LLM.
Then each option is judged independently for factual correctness, and
distractors that are actually true (typically a rephrasing of the correct
option, e.g. "the area under the pdf up to 5" vs "P(X ≤ 5)") are rewritten
into plausible but unambiguously false statements. This matters because MCQ
marking is a deterministic letter comparison — a student picking a
synonymous distractor would otherwise be wrongly given 0.

All verification failures are non-fatal and keep the original question.

*Math formatting* — generated text emits inline LaTeX (`$...$`). A
post-generation latexify pass (`math_format.py`) wraps any remaining bare math
(e.g. `P(x) = μ^x e^{-μ} / x!`) in `$`-delimiters and converts unicode/loose
notation to real LaTeX commands, with an `unmangle_latex` repair step for
malformed output; the frontend then renders it with KaTeX via the `MathText`
component. The rewrite is translation-only and sanity-checked, so wording and
numbers are preserved. Questions created before this pass can be reformatted via
`POST /admin/questions/latexify`.

*MCQ option parsing* — `_split_mcq_text` takes only the **first contiguous
A→B→C(→D) marker run**, so an answer-key block embedded after the options can't
leak its letters into the parsed options.

If the final count is below the requested count, the job's completion message
reports "Created N of M requested questions" so the shortfall is visible.

---

## Step 4 — Cross-Chapter Deduplication

After all chapters complete in parallel, all generated questions are compared across chapters using embedding cosine similarity. Questions too similar to one from a different chapter are dropped.

This catches cases like "What is the mean?" appearing in both Chapter 1 (descriptive statistics) and Chapter 6 (normal distribution).

---

## Step 5 — Storage

Questions are bulk-inserted into MongoDB with:
- Embedding (768-dim) for future dedup and semantic search
- `source_page_range` — which pages the question came from
- `bloom_level` — L1–L5 classification
- `difficulty` — easy / medium / hard
- `rubric` — per-mark grading criteria
- `model_answer` — full correct answer
- `correct_answer` — structured answer key for MCQ (`"A"`–`"D"`) and
  True/False (`"True"`/`"False"`); marking compares against this directly,
  with no model call and no re-parsing of the model answer prose. Never
  exposed through the student assessment endpoint.

---

## Question Format

Each generated question includes:

```json
{
  "question_text": "A bag contains 4 red and 6 blue marbles. What is P(red)?",
  "question_type": "short_answer",
  "model_answer": "P(red) = 4/10 = 0.4",
  "rubric": "1 mark: correct numerator/denominator setup. 1 mark: correct decimal.",
  "max_marks": 2,
  "difficulty": "easy",
  "bloom_level": "L3",
  "topic_tag": "Probability Topics",
  "source_page_range": "pp. 87-89"
}
```

A question may also carry **assets** (`assets[]`): a `table` (rendered as
deterministic HTML from the source markdown) or a `figure` (an AI-generated
chart redrawn from a stored figure description). When a source table is a
"find-the-missing-value" exercise, the blank body cell renders as a `?`
placeholder and a one-line note is appended to the caption, so the gap reads as
a deliberate prompt rather than an incomplete table.

---

## AI Provider

Generation uses **OpenAI `gpt-4o-mini`** as primary, falling back to **Anthropic `claude-haiku-4-5-20251001`** if OpenAI hits quota. The fallback is automatic — no configuration needed.

Gemini is not used for generation (it is used for embeddings only).

---

## Parallelism

- Up to **5 chapters generate in parallel** (controlled by `GEN_CHAPTER_CONCURRENCY`)
- Within each chapter, retrieval sub-queries run in parallel (`asyncio.gather`)
- Cross-chapter dedup runs after all chapters complete

Increasing `GEN_CHAPTER_CONCURRENCY` speeds up generation but increases API usage. With paid OpenAI keys, 5 is safe. Reduce to 2-3 if you hit rate limits.
