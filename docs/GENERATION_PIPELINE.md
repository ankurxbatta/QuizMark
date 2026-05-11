# How the question generation pipeline works

This is my notes on how the PDF-to-questions pipeline actually works under the hood, mostly so I remember what I built and can explain it.

---

## The basic flow

```
Upload PDF
    ↓
Parse into chunks (pdf_service.py)
    ↓
Score and select the best chunks (question_generator.py)
    ↓
Call the LLM for each chunk (question_generator.py)
    ↓
Validate and deduplicate output
    ↓
Save to database with embeddings
```

---

## Step 1 — Parsing the PDF into chunks

The main challenge with textbooks is that they have a lot of content you don't want to generate questions from — exercises, glossary sections, practice tests, table of contents, copyright pages, etc. The parser in `pdf_service.py` tries to handle this.

It uses `pdfplumber` to extract text page by page (with `pypdf` as fallback if pdfplumber fails). As it reads through the PDF it's tracking which chapter and section it's in.

**Chapter detection** uses a set of regex patterns because textbooks don't all format headings the same way:

- `Chapter 1 Sampling and Data`
- `Chapter 1: Sampling and Data`  
- `CHAPTER 1 SAMPLING AND DATA`
- `1 Sampling and Data` (no "Chapter" keyword)
- `1. Sampling and Data`

Section headings like `1.2 Measures of Spread` are also detected to give more granular chunk labels.

**Content filtering** — each accumulated block of text gets checked before it becomes a chunk:
- Blocks are scored for "teaching signals" — things like definitions, theorems, formulas, worked examples, mentions of statistical terms
- Blocks that look like exercises (lots of numbered items like `12. Calculate the mean...`) get discarded
- Blocks matching things like "Practice Test", "Chapter Review", "Homework", table of contents markers, etc. get skipped

Each chunk that makes it through becomes a `TextChunk` object with metadata: chapter number, chapter title, section title, topic tag, page range, whether it has formulas, whether it has worked examples, a teaching density score, and extracted key terms.

---

## Step 2 — Ranking and selecting chunks

Not all chunks are equally useful for generating questions. A dense theoretical section with formulas and worked examples will produce better questions than a brief introductory paragraph.

Each chunk gets a score from 0–1:

```
score = teaching_density × 0.5
      + 0.2 if it has formulas
      + 0.2 if it has worked examples  
      + 0.1 if it has more than 2 key terms
```

Chunks below 0.15 are thrown out. The rest are grouped by topic (chapter) and selected using a round-robin so the final set covers multiple chapters rather than just the highest-scoring one.

If the instructor filtered to a specific chapter, only chunks from that chapter are used.

---

## Step 3 — LLM generation

Each selected chunk gets sent to the LLM with a prompt that includes the chunk text, source metadata, and instructions for what the output should look like.

The LLM is told to return a JSON array where each question has:
- `question_text`
- `question_type`
- `model_answer`
- `rubric` — one criterion per mark, e.g. "1 mark: states the formula. 1 mark: correct interpretation."
- `max_marks` — 2 for basic recall up to 8 for multi-step problems
- `topic_tag`
- `difficulty` — easy, medium, or hard

Up to 3 chunks are processed at the same time (concurrent requests) to speed things up without hammering the API.

**If the LLM fails or returns bad JSON**, there's a fallback that generates basic questions deterministically from the text — things like fill-in-the-blank from key terms or true/false from source sentences. These are pretty low quality (always `easy`, `max_marks=2`) but better than nothing.

---

## Step 4 — Validation and cleanup

The raw LLM output goes through a validation pass:
- Checks all required fields are present
- Normalises field types (e.g. `max_marks` should be a float, `difficulty` should be one of three values)
- Drops any questions with empty `question_text` or `model_answer`
- Deduplicates by comparing the first 60 characters of each question text

Then the list gets trimmed to the requested count.

---

## Text file uploads

For `.txt` uploads there's no chunking — the text goes straight into a single generation prompt. Works fine for short content but obviously doesn't have chapter detection or content filtering.

---

## Async jobs for large textbooks

For big PDFs the `/generate/async` endpoint kicks off a Celery background job (`ingest_tasks.py`) instead of blocking the HTTP request. The job runs the same pipeline chapter by chapter. You poll `/questions/jobs/{job_id}` to check how it's going — it tracks how many chapters are done and how many questions have been created so far.

---

## Embeddings

After each question is saved to the database, an embedding is generated from the question text + model answer using `nomic-embed-text` (running locally via Ollama). This gets stored in a `pgvector` column. I added this mostly for future use — the idea being you could search questions semantically or detect duplicates across different generation runs.
