# Architecture

## Overview

QuizMark is a containerised web application built on FastAPI + Next.js. All heavy processing is done by specialised Celery workers, not the API server. The API server only validates requests and hands tasks to Redis queues.

```
Browser → Next.js (3000) → FastAPI (8000) → Redis broker
                                                  │
                           ┌──────────────────────┼──────────────────────┐
                           │                      │                      │
                     worker-ingest         worker-gen            worker-mark
                     worker-vision         worker-deepsearch     worker-clean
                     worker-math           worker-embed
                           │                      │                      │
                           └──────────────────────┼──────────────────────┘
                                                  │
                                              MongoDB
                                   (data store + 768-dim vector index)
```

---

## Container Map

| Container | Port | Role |
|---|---|---|
| `frontend` | 3000 | Next.js UI (instructor + student) |
| `backend` | 8000 | FastAPI REST API |
| `mongodb` | 27017 | Primary data store + vector search |
| `broker` (Redis) | 6379 | Celery task queue |
| `mongo-express` | 8081 | Database browser UI |
| `flower` | 5555 | Celery worker monitor |
| `worker-ingest` | — | PDF parsing, resumable ingestion |
| `worker-vision` | — | Chart/image descriptions |
| `worker-math` | — | LaTeX formula extraction |
| `worker-clean` | — | PDF noise removal |
| `worker-embed` | — | Vector embeddings |
| `worker-deepsearch` | — | Multi-query RAG retrieval |
| `worker-gen` | — | Question generation |
| `worker-mark` | — | Answer marking |

---

## Worker Queues and Concurrency

Each worker subscribes to exactly one Redis queue. They all share the same Docker image — only the startup command differs.

| Worker | Queue | Concurrency | Notes |
|---|---|---|---|
| worker-ingest | ingest_tasks | 2 | Memory-heavy (full PDF in RAM) |
| worker-vision | vision_tasks | 1 | Rate-limited by vision API |
| worker-math | math_tasks | 2 | OpenAI → Anthropic fallback |
| worker-clean | clean_tasks | 4 | CPU-only, no API calls |
| worker-embed | embed_tasks | 3 | Gemini → OpenAI fallback |
| worker-deepsearch | deepsearch_tasks | 3 | Parallel vector searches |
| worker-gen | gen_tasks | 2 | Longest-running tasks |
| worker-mark | mark_tasks | 4 | High throughput for submissions |

---

## Ingestion Pipeline

```
PDF Upload (up to 25 MB, 700 pages)
        │
        ▼
    backend          — hash PDF, create ingest job in MongoDB
        │
        │  ingest_book_resumable_task → ingest_tasks queue
        ▼
  worker-ingest      — processes 6 pages at a time through the
                       LangChain (LCEL) ingestion chain:

    ┌─ Parse pages (PyMuPDF) ─────────────── sync
    │    text · tables · math fonts · vector graphics detected
    │
    ├─ Text cleaning ─────────────────────── sync, in-process
    │    ligatures · mojibake · boilerplate · page-number noise stripped
    │
    ├─ Chunking ──────────────────────────── recursive split + semantic re-split
    │    short tails merged (never dropped) · top 20% by teaching density
    │    re-split at semantic boundaries
    │
    ├─ Chunk validation ──────────────────── LLM math repair, cached by content hash
    │    restores operators lost in PDF extraction · dedups content
    │    across text / tables / math
    │
    ├─ Vision + Math API calls ───────────── semaphore-metered asyncio.gather
    │    chart pages → description · math-font pages → LaTeX
    │    provider fallback chain: OpenAI → Anthropic → Gemini
    │
    ├─ Batch embeddings ──────────────────── EMBEDDING_BATCH_SIZE texts per call
    │    Gemini gemini-embedding-001 (768-dim) → OpenAI fallback (same 768-dim)
    │    failed batches fall back to per-item embedding
    │
    ├─ MongoDB bulk insert ───────────────── deterministic chunk IDs
    │    (re-inserts after a crash are duplicate-key no-ops)
    │
    └─ Checkpoint saved (next_page + accumulator state)
         │
         └─▶ next 6 pages ... repeat until complete

Re-uploading the same PDF resumes from the last checkpoint automatically.
If a window fails mid-flight, its partial chunks are rolled back on resume
and the same window is retried — no orphaned or duplicated chunks.

Both ingestion paths (one-shot upload+generate and resumable book-only)
run through the same chain.

Each stage reports progress to the ingest job ("Pages 7–12: reading
formulas and charts with vision AI…"), so the UI shows live pipeline
activity even while a multi-minute window is in flight.
```

After the final window, specialist index builds (math / figure / table) are
enqueued. Their status and progress are stored in `index_build_jobs` and
surfaced on the Library book card until they finish.

---

## Question Generation Pipeline

```
Instructor requests generation
        │
        ▼
    backend  →  generate_from_book_task  →  gen_tasks queue
        │
        ▼
  worker-gen    — fetches chapter list from MongoDB

  Up to 5 chapters run in parallel (asyncio.Semaphore):

    Round 0:  extract_chapter_concepts
              LLM decomposes topic into key concepts + retrieval sub-queries

    Round 1:  deep_retrieve (4 parallel MongoDB vector searches)
              → surfaces most testable chunks by teaching density
              → generates ~70% of target count across Bloom's levels
                 L1 recall · L2 understand · L3 apply · L4 analyse · L5 evaluate

    Round 2:  Bloom's coverage audit
              → for each under-represented level: targeted retrieval + generation

    Round 3:  dedup + validate
              → drops near-duplicates (cosine similarity ≥ 0.92)
              → enforces Bloom's distribution

    Round 4:  numeric answer verification (answer_verifier.py)
              → model answers containing calculations are recomputed in a
                dedicated step-by-step LLM pass; wrong values are rewritten
                so the marker is never fed arithmetic errors

  Cross-chapter dedup → bulk insert into MongoDB questions collection

  MCQ and True/False questions are stored with a structured `correct_answer`
  key (the letter A–D, or "True"/"False") extracted at generation time.
  If fewer questions than requested survive dedup/validation, the job's done
  message reports "Created N of M requested questions" instead of failing
  silently.
```

---

## Marking Pipeline

```
Student submits answer  (one submission per question — duplicates get 409)
        │
        ▼
  worker-mark (concurrency=4)

    MCQ / True-False  — deterministic, no model call
        │   compared directly against the stored correct_answer key
        │   (falls back to parsing the model answer for legacy questions;
        │    if no key can be determined the submission is flagged for
        │    instructor review instead of silently scoring 0)
        ▼
    SLM pre-scorer  — fast cosine similarity check (short answer only)
        │
        ├── HIGH confidence (≥ 0.85) → mark accepted, no LLM call
        │
        └── LOW / MID confidence
                │
                ├─ Multi-query RAG (3 parallel vector searches)
                │    · textbook chunks (pdf_chunks)
                │    · similar Q&A pairs (questions)
                │
                └─ LLM marking
                     Input: question + rubric + model answer + context
                     Provider: OpenAI gpt-4o-mini → Anthropic claude-haiku
                     Output: mark + written feedback → saved to MongoDB

                     The model answer is treated as a guide, not ground
                     truth: the marker recomputes calculations itself and,
                     when a mathematically correct student answer conflicts
                     with the model answer, awards the marks and flags the
                     question for instructor review.
```

---

## AI Provider Fallback Chains

`api_key_manager` tracks quota health per provider. On 429 or quota exhaustion, the provider enters cooldown (60s rate-limit / 1hr quota) and the next provider is used automatically.

| Capability | Chain |
|---|---|
| Embeddings (768-dim) | Gemini `gemini-embedding-001` → OpenAI `text-embedding-3-small` |
| Vision / charts | OpenAI `gpt-4o-mini` → Anthropic `claude-haiku` |
| Math extraction | OpenAI `gpt-4o-mini` → Anthropic `claude-haiku` |
| Question generation | OpenAI `gpt-4o-mini` → Anthropic `claude-haiku` → Gemini |
| Answer marking | OpenAI `gpt-4o-mini` → Anthropic `claude-haiku` → Gemini |

Both OpenAI and Anthropic produce 768-dim embeddings via `dimensions=768`, keeping the MongoDB vector index compatible across all providers.

---

## MongoDB Collections

| Collection | Purpose |
|---|---|
| `users` | Instructor and student accounts |
| `pdf_chunks` | Textbook content with 768-dim embeddings |
| `questions` | Generated question bank (incl. `correct_answer` key for MCQ/TF) |
| `submissions` | Student answers and marking results (unique per student+question) |
| `ingest_jobs` | Job progress and status |
| `ingest_checkpoints` | Resumable ingestion state (keyed by PDF SHA-256 hash) |
| `math_index` / `figure_index` / `table_index` | Specialist RAG indexes |
| `index_build_jobs` | Specialist index build status + progress |
| `audit_logs` | Login and action history |

All vector indexes use cosine similarity (768 dimensions). Collections and
their vector search indexes are created at backend startup **and** ensured
again before the first chunk write in each worker process — so a fresh
database works on the very first ingestion, with no backend restart needed.

---

## Text Cleaning Pipeline

`text_cleaner.py` normalises each chunk before embedding:

1. Mojibake repair (UTF-8 bytes misread as Latin-1)
2. Ligature expansion (ﬁ→fi, ﬂ→fl, ﬀ→ff, ﬃ→ffi, ﬄ→ffl)
3. Smart quote and dash normalisation
4. Zero-width / control character removal
5. NFC Unicode normalisation
6. Soft-hyphen line-break joining
7. OpenStax boilerplate removal
8. Page number / chapter header noise removal
9. Duplicate line deduplication
10. Whitespace collapse

Trigger a full re-clean on demand:
```bash
curl -X POST http://localhost:8000/api/v1/admin/clean/all \
  -H "Authorization: Bearer <token>"
```
