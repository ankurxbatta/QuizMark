# Multi-Specialist RAG Architecture — Design

Status: **APPROVED 2026-06-11 — Phases 1–3 implemented.**
Phase 1: math_index + builder on worker-math, vector_search generalisation + book_id
pre-filter fix, auto-backfill, admin endpoints, KEY FORMULAS in L3 generation.
Phase 2: figure_index (worker-vision) + table_index (worker-clean) + L4 visual/tabular
routing (FIGURES/TABLES prompt blocks in Analyze-level generation).
Phase 3: retrieval_router (heuristic intents → specialist search → RRF fusion →
parent-chunk cross-link expansion) wired into generation retrieval; heuristic-routed
specialist context in marking; rebuild_index_embeddings_task (worker-embed) and
multi_index_retrieve_task (worker-deepsearch). All 8 workers now serve their
intended specialist roles. Remaining: Phase 4 (optional — rerankers, eval harness).
Decisions: auto-backfill on deploy · heuristic-only marking routing · keep all 8 workers
(1:1 specialist mapping) · `table_index` pulled into Phase 2
Author: design session 2026-06-11

---

## 1. Intent and Goals

The original worker architecture (vision / math / embed / deepsearch) was conceived as a set of
**specialist RAG agents** — each owning one content modality, connected strategically. The
ingestion-chain rework optimised the write path but absorbed the specialists in-process, leaving
the read path generic: every query searches one fused index.

This design completes the original intent:

- Each modality (prose, formulas, figures) gets **its own index with its own embedding strategy**
- A **router** decomposes queries by intent and sends each sub-query to the right specialist
- Results are **fused** (reciprocal-rank fusion) and **expanded across cross-links**
  (formula ↔ parent chunk ↔ figure on the same page)
- The idle workers come back online as **asynchronous index builders** — their architecturally
  correct role
- Everything reuses content already paid for during ingestion (repaired LaTeX, vision
  descriptions); no re-ingestion needed

**Non-goals:** changing the marking confidence router; cross-book reasoning; frontend redesign
(only small additions); replacing the ingestion chain (write path semantics unchanged).

**Key safety property:** if any specialist index is empty, missing, or failing, retrieval
degrades to exactly today's behaviour (text-only). Rollout is zero-risk.

---

## 2. Current State (read path)

- One collection (`pdf_chunks`), one 768-dim cosine vector index; a chunk's prose, LaTeX,
  table rows and chart descriptions are concatenated into a single embedding
- Generation: `deep_retrieve_for_generation` (question_generator.py:142) — 4 LLM sub-queries →
  parallel `vector_search` → dedupe by `_id`
- Marking: `_retrieve_context` (rag_pipeline.py:133) — 3 heuristic concept queries → same fan-out
- Modality data already exists per chunk: `math_text` (LLM-repaired LaTeX), `image_texts`
  (vision chart descriptions), `table_texts`, `has_formula`, `graph_page_nums`, `math_page_nums`
- **Known flaw to fix here:** `vector_search` applies `book_id` as a `$match` *after*
  `$vectorSearch`, so a k-result search over the wrong book returns fewer than k (sometimes zero)
  relevant results. The filter must move inside `$vectorSearch.filter`, which requires declaring
  filter fields in the index definition.

---

## 3. Data Model

### 3.1 `math_index` — one document per formula occurrence

| Field | Content |
|---|---|
| `_id` | `sha1(book_hash:parent_chunk_id:normalised_formula)[:24]` — deterministic, idempotent |
| `book_id`, `book_hash`, `parent_chunk_id` | provenance + cross-link |
| `chapter_num`, `chapter_title`, `section_title`, `page` | location |
| `formula_latex` | repaired LaTeX (from chunk_validator output) |
| `formula_plain` | plain-text rendering |
| `context_sentence` | the sentence(s) surrounding the formula in prose |
| `concept_label` | short LLM label, e.g. "sample standard deviation" |
| `variables` | `[{symbol, meaning}]` — LLM-extracted |
| `embedding` | 768-dim of `"{concept_label}: {formula_plain} — {context_sentence}"` |

Vector index `math_vector_index` with filter fields `book_id`, `chapter_num`.

### 3.2 `figure_index` — one document per figure/chart

| Field | Content |
|---|---|
| `_id` | `sha1(book_hash:page:figure_ordinal)[:24]` |
| `book_id`, `book_hash`, `parent_chunk_id`, `page` | provenance + cross-link |
| `figure_kind` | `histogram \| scatter \| boxplot \| bar \| table-figure \| other` (LLM-classified) |
| `description` | vision description (already produced at ingestion — reused, not re-paid) |
| `axis_summary` | one-liner: axes, units, visible trend (LLM, derived from description) |
| `caption` | nearby caption text when detected |
| `chapter_num`, `chapter_title`, `section_title` | location |
| `embedding` | 768-dim of `"{figure_kind} — {caption} — {description}"` |

Vector index `figure_vector_index` with filter fields `book_id`, `chapter_num`, `figure_kind`.

### 3.3 Cross-links

Every specialist document carries `parent_chunk_id` → `pdf_chunks._id`. `pdf_chunks` itself is
**not** migrated (no new fields); reverse lookups (chunk → its formulas/figures/tables) are
queries on the specialist collections, which carry a `parent_chunk_id` index.

### 3.4 `table_index` — one document per table (Phase 2)

| Field | Content |
|---|---|
| `_id` | `sha1(book_hash:parent_chunk_id:table_ordinal)[:24]` |
| `book_id`, `book_hash`, `parent_chunk_id`, `page` | provenance + cross-link |
| `table_markdown` | the extracted markdown table (from `table_texts`) |
| `table_summary` | one-line LLM summary: what the table shows, key columns |
| `headers` | extracted column headers |
| `chapter_num`, `chapter_title`, `section_title` | location |
| `embedding` | 768-dim of `"{table_summary} — {headers} — first rows"` |

Vector index `table_vector_index` with filter fields `book_id`, `chapter_num`. Built by
`build_table_index_task` on the `clean_tasks` queue (worker-clean — CPU-light, queue has
spare capacity; tables need only one small LLM summary call each).

---

## 4. Index Building — the workers' new jobs

### 4.1 Tasks and queue mapping

| Task | Queue | Worker |
|---|---|---|
| `build_math_index_task(book_id)` | `math_tasks` | worker-math |
| `build_figure_index_task(book_id)` | `vision_tasks` | worker-vision |
| `rebuild_index_embeddings_task(index, book_id)` | `embed_tasks` | worker-embed |
| `multi_index_retrieve_task(...)` (optional prefetch) | `deepsearch_tasks` | worker-deepsearch |

All four formerly idle workers regain real, queue-shaped (retroactive, per-book, independent) jobs.

### 4.2 Triggers

1. **On ingest completion** — when the resumable ingest finishes a book, it enqueues
   `build_math_index_task` + `build_figure_index_task` for that book. Per-book (not per-window):
   builders read *stored chunks*, need no PDF access, and books finish in one or two builds.
2. **Auto-backfill on deploy** — at backend startup, books that are fully ingested but missing
   specialist indexes get builds enqueued automatically (guarded by a per-book `index_build_jobs`
   marker so restarts don't re-enqueue). Existing libraries become specialist-searchable without
   manual action; one-time cost of a few cents per existing book.
3. **Manual rebuild** — `POST /admin/index/build/{book_id}` (and `/admin/index/build-all`),
   instructor-only, for rebuilds after prompt/enrichment improvements.
3. **Idempotency** — deterministic `_id`s + upserts; a rebuild deletes the book's specialist docs
   then rebuilds. Build progress recorded in an `index_build_jobs` doc so the UI can show status.

### 4.3 Builder logic

**Math builder:** read book chunks where `has_formula` or `math_text != ""` → split into individual
formulas (line breaks + LaTeX delimiters) → batched LLM enrichment (10 formulas/call →
`{concept_label, variables, formula_plain}`), cached by content sha1 in `index_build_cache`
(same pattern as `validation_cache`) → batched embeddings (`EMBEDDING_BATCH_SIZE`) → bulk upsert.

**Figure builder:** read chunks with `image_texts` → one doc per description → batched LLM
classification (`figure_kind` + `axis_summary`), cached → embed → upsert.

**Cost (600-page statistics text):** ~300–600 formulas + ~150–300 figures ≈ 50–100 small batched
LLM calls + ~1k embeddings ≈ **a few cents per book**, one-time (cache makes rebuilds near-free).

---

## 5. Retrieval Layer — the strategic connection

New service `backend/app/services/retrieval_router.py`:

```
routed_retrieve(queries, book_id, k, intents=None) -> FusedContext
```

1. **Intent decomposition** — sub-queries tagged `conceptual | computational | visual`.
   - Generation path: one LLM call (extends the existing `_generate_retrieval_queries` —
     it already makes this call, so this adds tagging, not a new call).
   - Marking path: **heuristics only, no LLM** (formula symbols / "calculate" → computational;
     "graph, chart, figure, distribution, skew" → visual) — marking latency must not grow.
2. **Specialist search** — parallel vector search per intent:
   conceptual → `pdf_chunks`, computational → `math_index`, visual → `figure_index`.
   Requires generalising `mongo_vector_store.vector_search(collection, index, filters)`
   (backwards-compatible wrapper keeps the current signature) **and** moving `book_id` into
   `$vectorSearch.filter` (fixes the post-filter recall bug for all callers).
3. **Fusion** — Reciprocal-Rank Fusion across result lists: `score = Σ 1/(RRF_K + rank)`,
   deduped by `parent_chunk_id` so a formula and its parent chunk don't double-count.
4. **Cross-link expansion** — for top specialist hits, fetch the parent chunk; for top text hits,
   fetch up to `EXPANSION_NEIGHBORS` linked formulas/figures.
5. **Structured output** — `FusedContext {text_chunks, formulas, figures}` with `to_prompt()`
   rendering distinct sections: `TEXTBOOK CONTEXT` / `KEY FORMULAS` / `FIGURES`.

Every specialist search is individually try/except-guarded → any failure degrades that intent to
text retrieval.

---

## 6. Integration Points

### 6.1 Question generation (largest payoff)

- `deep_retrieve_for_generation` routes through `routed_retrieve`.
- **Bloom-level → modality routing** in the orchestrator's gap-fill rounds:

| Bloom level | Routed intent | Effect |
|---|---|---|
| L1 Remember / L2 Understand | conceptual | unchanged (text) |
| L3 Apply | computational priority | prompt gets exact repaired LaTeX in `KEY FORMULAS` → numerically correct computational questions |
| L4 Analyze | visual priority | prompt gets real figure descriptions → data-interpretation questions about actual charts |
| L5 Evaluate | conceptual + visual mix | richer judgment questions |

  Today the gap-fill rounds *hope* the right modality surfaces from the fused index; after this
  they ask for it directly.

### 6.2 Marking

`_retrieve_context` adds heuristic-routed specialist context: if the question/rubric/answer
contains formula signals, the marker also receives the canonical formula from `math_index`
(`KEY FORMULAS` block) to check the student's work against; figure-referencing questions get the
figure description. Confidence router unchanged; no added LLM calls on the marking path.

### 6.3 Admin / lifecycle

- `POST /admin/index/build/{book_id}`, `POST /admin/index/build-all` — enqueue builds (instructor)
- `GET /admin/index/status` — per-book doc counts and last build per index
- Book deletion cascades to `math_index` + `figure_index` docs (extends existing delete-book flow)

---

## 7. Configuration

```
MATH_INDEX_ENABLED=true        # feature flag per specialist
FIGURE_INDEX_ENABLED=true
ROUTER_LLM_DECOMPOSITION=true  # generation path only; marking always heuristic
RRF_K=60                       # standard RRF constant
INDEX_BUILD_BATCH_SIZE=10      # formulas/figures per enrichment LLM call
EXPANSION_NEIGHBORS=2          # cross-link expansion cap per hit
```

---

## 8. Failure Handling

- **Builders:** per-book Celery task, `max_retries=2`; idempotent upserts make partial builds
  safe; status in `index_build_jobs`. A failed build leaves retrieval working (text-only for the
  missing modality).
- **Router:** per-specialist try/except → graceful degradation to today's behaviour. Feature
  flags allow disabling any index instantly.
- **Index rebuilds after prompt improvements:** `rebuild_index_embeddings_task` re-enriches and
  re-embeds without re-ingesting (the cache makes unchanged content free).

---

## 9. Worker Fleet After This Design

| Worker | Queue | Role |
|---|---|---|
| worker-ingest | ingest_tasks | LCEL chain (unchanged) + enqueues index builds on completion |
| worker-math | math_tasks | **math index builder** |
| worker-vision | vision_tasks | **figure index builder** |
| worker-embed | embed_tasks | **index embedding rebuilds** |
| worker-deepsearch | deepsearch_tasks | **queue-shaped retrieval prefetch** (optional) |
| worker-gen | gen_tasks | unchanged |
| worker-mark | mark_tasks | unchanged |
| worker-clean | clean_tasks | unchanged |

All 8 containers justified again, in their originally intended roles. (If resources are tight,
the build queues can be consumed by fewer containers with zero code change — routing is
preserved.)

---

## 10. Phasing

| Phase | Scope | Ships independently |
|---|---|---|
| **1** | `math_index` + builder task + `vector_search` generalisation **+ pre-filter fix** + auto-backfill on startup + `KEY FORMULAS` in generation L3 + admin build/status endpoints + tests | yes — biggest single win for statistics texts |
| **2** | `figure_index` + `table_index` + builders + L4 visual/tabular routing + tests | yes |
| **3** | RRF fusion + cross-link expansion + marking integration (heuristic routing) | yes |
| **4** (optional) | per-specialist rerankers, retrieval evaluation harness (golden-query set per book) | yes |

Each phase leaves the system fully working; flags default new behaviour on with automatic
degradation.

---

## 11. Decisions (resolved 2026-06-11)

1. **Vector index rebuild** — accepted as required for correctness: `book_id` moves into
   `$vectorSearch.filter`; Atlas Local rebuilds the index in the background (brief window of
   degraded search on first deploy).
2. **Marking routing** — heuristic-only (no LLM call on the marking path).
3. **Worker mapping** — keep all 8 containers, 1:1 specialist mapping as originally designed.
4. **Backfill** — automatic on deploy for all ingested books missing indexes.
5. **Tables** — `table_index` pulled into Phase 2 (built on `clean_tasks` / worker-clean).
