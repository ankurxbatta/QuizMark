# Question Generation — Detailed Architecture

How a "Generate ~N Questions" click becomes verified questions in the bank.
Companion to [GENERATION_PIPELINE.md](GENERATION_PIPELINE.md) (conceptual
walkthrough); this document is the component-level map, traced from code.

---

## 1. End-to-end flow

```mermaid
flowchart TD
    UI["Instructor UI<br/>library/[book_id] page"] -->|POST /questions/generate/from-book| API["FastAPI backend<br/>api/v1/questions.py"]
    API -->|creates job doc in ingest_jobs| MDB[(MongoDB)]
    API -->|generate_from_book_task.delay| Q[["Redis · gen_tasks queue"]]
    Q --> WG["worker-gen<br/>tasks/ingest_tasks.py"]

    WG --> RGB["_run_generate_from_book<br/>discover chapters via $group on pdf_chunks"]
    RGB --> RCP["_run_chapters_parallel<br/>asyncio.Semaphore(GEN_CHAPTER_CONCURRENCY=5)"]

    RCP --> CH1["_generate_chapter (Ch 4)"]
    RCP --> CH2["_generate_chapter (Ch 5)"]

    CH1 --> ORCH["orchestrate_question_bank<br/>services/question_orchestrator.py"]
    CH2 --> ORCH

    ORCH --> R0["Round 0 — concept extraction"]
    R0 --> R1["Round 1 — broad generation (~70%)"]
    R1 --> R2["Round 2 — Bloom's gap fill (non-fatal)"]
    R2 --> R2B["Round 2b — concept coverage fill (non-fatal)"]
    R2B --> R3["Round 3 — dedup + balance + top-up"]
    R3 --> R4["Round 4 — quality verification<br/>services/answer_verifier.py"]

    R4 --> NORM["_normalise_q (field allow-list)<br/>+ embed_batch(question_text + model_answer)"]
    NORM --> XDEDUP["_dedup_across_chapters<br/>cosine ≥ DEDUP_SIMILARITY_THRESHOLD (0.92)"]
    XDEDUP --> INS["bulk insert → questions collection<br/>incl. correct_answer key for MCQ/TF"]
    INS --> JOB["job → done<br/>'Created N of M requested questions' on shortfall"]
    JOB -->|SSE stream / polling| UI
```

The UI watches the job over the SSE endpoint
(`GET /questions/jobs/{id}/stream`), falling back to 5-second polling.

---

## 2. Retrieval: DeepSearch + multi-index routing

Every generation round retrieves through the same stack
(`deep_retrieve_for_generation` → `retrieval_router.routed_retrieve`):

```mermaid
flowchart LR
    T["topic<br/>(chapter title / gap suffix / concept)"] --> SQ["_generate_retrieval_queries<br/>LLM writes 4 exam-focused sub-queries"]
    SQ --> E["embed sub-queries (parallel)"]
    E --> RT{"intent router<br/>per sub-query"}
    RT -->|always| C[("pdf_chunks<br/>vector index")]
    RT -->|formula intent| M[("math_index")]
    RT -->|figure intent| F[("figure_index")]
    RT -->|table intent| TB[("table_index")]
    M & F & TB -->|cross-links pull<br/>parent chunks in| C
    C --> RRF["RRF fusion (k = RRF_K)<br/>rank-merge all result lists"]
    RRF --> OUT["top-k chunks ranked by teaching_density<br/>+ top formulas / figures / tables"]
```

Specialist hits matter twice: they re-rank their **source chunks** into the
fused result (cross-links), and the raw formulas/figures/tables feed the
targeted prompts in Round 2 (L3 gets verbatim LaTeX, L4 gets real
figures/tables — `_specialist_context`).

---

## 3. The orchestrator rounds

| Round | Function | What it does | On failure |
|---|---|---|---|
| 0 | `extract_chapter_concepts` | LLM decomposes the chapter into key concepts + enriched topic string | falls back to bare topic |
| 1 | `generate_questions_from_chunks` | retrieve top chunks → select by `_score_chunk` (teaching density, formulas, examples) → up to 3 chunks generate concurrently with `_PLAIN_TEXT_PROMPT` (Bloom's guide + 4-level uniqueness block) → prefix dedup → `_validate_questions` | chapter fails only if Round 1 yields nothing |
| 2 | `_audit_bloom_gaps` → `generate_targeted_bloom_questions` | for each under-represented Bloom level: focused retrieval (`retrieval_suffix + topic`) and generation locked to that level, augmented with specialist context | **non-fatal** — gap skipped, accumulated questions kept |
| 2b | concept coverage | up to 5 uncovered Round-0 concepts get one targeted L3 question each | **non-fatal** per concept |
| 3 | `_dedup_by_prefix` + `_balance_bloom_distribution` | trim duplicates and over-represented levels; top-up pass if > 20% below target | top-up best-effort |
| 4 | `verify_generated_questions` | see §4 | **non-fatal** per question |

`_validate_questions` (Round 1/2 output) is also where structure is enforced:
`_normalise_mcq` rebuilds MCQs as stem + A–D options, extracts the
`correct_answer` letter, and tags any placeholder-filled options with
`_generic_distractors` for Round 4 to replace. True/False answers get
`correct_answer` parsed from the model answer.

---

## 4. Round 4 — quality verification (`answer_verifier.py`)

```mermaid
flowchart TD
    Qs["validated questions"] --> SPLIT{type?}

    SPLIT -->|short_answer with numbers| NV["numeric verification"]
    NV --> EX["LLM extracts final calculation as a<br/>pure-Python expression + stated value<br/>(translation, not arithmetic)"]
    EX --> EV["evaluate_expression()<br/>restricted eval: math fns only,<br/>charset whitelist, exponent/factorial caps"]
    EV --> CMP{"|computed − stated| ≤ 2%?"}
    CMP -->|yes| OK1["keep"]
    CMP -->|no| RW["LLM rewrites model answer with the<br/>deterministically computed value<br/>(rewrite must contain that value)"]

    SPLIT -->|mcq| MV["MCQ verification"]
    MV --> GD["_fill_generic_distractors<br/>replace tagged boilerplate options with<br/>topic-specific false distractors"]
    GD --> J["judge: is each option independently<br/>a factually correct answer?"]
    J --> AMB{"extra options judged true?"}
    AMB -->|no| OK2["keep"]
    AMB -->|yes| FX["rewrite those distractors to be<br/>plausible but unambiguously false"]
    J --> KEYFAIL{"stored key judged false?"}
    KEYFAIL -->|yes| LOGONLY["log warning only — a single LLM<br/>opinion never overturns the key"]
```

Why the split design for numeric checks: LLMs are unreliable at arithmetic
but reliable at *translation*. The number that ends up in the stored model
answer always comes from Python, never from the model. (Observed failure
that motivated this: the LLM "recomputed" C(20,12)·0.35¹²·0.65⁸ as 0.0515;
the true value is 0.0136.)

Why MCQs must be unambiguous: marking compares the student's letter against
`correct_answer` deterministically — there is no model at marking time to
notice that a distractor was synonymous with the right answer.

---

## 5. Marking-time contract

What generation guarantees the marking pipeline can rely on:

| Field | Guarantee |
|---|---|
| `correct_answer` | present for MCQ ("A"–"D") and True/False ("True"/"False"); never exposed via the student assessment endpoint |
| `model_answer` | numeric results deterministically verified (short answer) |
| MCQ options | exactly one factually correct option |
| `embedding` | 768-dim, embedded from `question_text + model_answer` |
| `rubric` | per-mark criteria (used by SLM pre-scorer + LLM marker) |

---

## 6. Sequence (happy path, one chapter)

```mermaid
sequenceDiagram
    participant UI as Instructor UI
    participant API as FastAPI
    participant W as worker-gen
    participant LLM as LLM providers
    participant DB as MongoDB

    UI->>API: POST /generate/from-book?book_id&type&count
    API->>DB: insert ingest_jobs doc (queued)
    API->>W: generate_from_book_task (Redis)
    API-->>UI: job_id
    UI->>API: SSE /jobs/{id}/stream

    W->>DB: $group pdf_chunks → chapter list
    W->>LLM: Round 0 concept extraction
    W->>DB: Round 1 multi-index vector search (RRF)
    W->>LLM: Round 1 generation (≤3 chunks parallel)
    W->>LLM: Round 2/2b targeted gap fill (non-fatal)
    W->>LLM: Round 4 verify (numeric expr + MCQ judge)
    W->>W: evaluate_expression (deterministic)
    W->>LLM: embed_batch(questions)
    W->>DB: bulk insert questions (+correct_answer)
    W->>DB: job → done ("Created N of M…")
    DB-->>UI: SSE: done, questions_created
```

---

## 7. Tuning knobs

| Setting | Default | Effect |
|---|---|---|
| `GEN_CHAPTER_CONCURRENCY` | 5 | chapters generated in parallel; lower to 2–3 on rate-limited keys |
| `DEDUP_SIMILARITY_THRESHOLD` | 0.92 | cross-chapter cosine dedup cut-off |
| `RRF_K` | (config) | rank-fusion constant for multi-index retrieval |
| `count_per_chapter` | request param (1–50) | target per chapter; shortfall is reported, not silent |

Failure semantics in one line: **a chapter only fails if Round 1 produces
nothing; everything after Round 1 is enrichment and degrades gracefully.**
Transient provider errors (408/429/431/5xx) retry with backoff inside every
client before any of this logic sees them.
