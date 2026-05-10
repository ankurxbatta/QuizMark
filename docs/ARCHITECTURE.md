# Hybrid SLM + RAG + LLM Architecture

## Design Philosophy

The goal is to be **as fast and private as possible** while being **as accurate as necessary**.

Most student answers to structured rubric questions are either clearly correct, clearly incorrect, or clearly partial — the full LLM is not needed for them. Only genuinely ambiguous answers need expensive inference. The hybrid pipeline exploits this by routing each answer to the cheapest model that can handle it confidently.

---

## Three-Tier Pipeline

```
Student answer
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  Tier 1 — SLM Pre-scorer  (phi3:mini, ~2s on CPU)  │
│                                                     │
│  Signal 1: Keyword coverage    (rubric keywords)    │
│  Signal 2: Semantic similarity (cosine vs stored    │
│            model-answer embedding)                  │
│  Signal 3: SLM quick score     (0-10 integer,       │
│            deterministic temp=0.0)                  │
│                                                     │
│  Blend: 30% keywords + 40% semantic + 30% SLM      │
│  → Confidence score [0.0 – 1.0]                    │
└────────────────┬────────────────────────────────────┘
                 │
                 ▼
         ┌───────────────┐
         │ Confidence    │
         │ router        │
         └──┬────┬───────┘
            │    │
     ≥0.85  │    │  0.55–0.85       < 0.55
            │    │                      │
            ▼    ▼                      ▼
    ┌──────────┐  ┌──────────────────┐  ┌─────────────────────┐
    │  HIGH    │  │      MID         │  │       LOW           │
    │          │  │                  │  │                     │
    │ Accept   │  │ RAG top-5        │  │ RAG wide top-10     │
    │ SLM mark │  │ Offline LLM      │  │ Online LLM (opt.)   │
    │ No LLM   │  │ (llama3)         │  │ or offline LLM      │
    │ ~2s      │  │ ~15s             │  │ Auto-flagged        │
    └────┬─────┘  └────────┬─────────┘  └──────────┬──────────┘
         │                 │                        │
         └────────┬────────┘────────────────────────┘
                  ▼
         mark + feedback + flagged + route + confidence
                  │
                  ▼
         Submission table (Postgres)
```

---

## Tier 1 — SLM Pre-scorer

**Model:** `phi3:mini` (Microsoft, ~2.3 GB, runs well on CPU)

**Why phi3:mini?**
- 3.8B parameters, instruction-tuned specifically for reasoning tasks
- Returns structured integer output reliably
- Temperature 0.0 → deterministic, reproducible scores
- Runs in ~1–2s on CPU vs 20–60s for llama3

**Three signals explained:**

| Signal | Method | Weight |
|--------|--------|--------|
| Keyword coverage | Fraction of non-stop rubric keywords present in the answer | 30% |
| Semantic similarity | Cosine distance between answer embedding and stored model-answer embedding (nomic-embed-text) | 40% |
| SLM quick score | phi3:mini asked for a single integer 0-10 with a rubric-anchored prompt | 30% |

Semantic similarity is weighted highest because it captures meaning rather than just surface vocabulary, and the embedding is already computed and stored when the question is created — the only extra cost is embedding the student answer (~0.1s).

---

## Tier 2 — RAG Retrieval

**Model:** pgvector (PostgreSQL extension)
**Embedding:** nomic-embed-text (768-dimensional)

The retrieval layer provides the LLM with examples of similar questions and their correct model answers. This acts as few-shot context, dramatically improving the quality of rubric-guided marking for smaller models.

| Path | K (similar answers retrieved) |
|------|-------------------------------|
| MID  | 5 (focused, high-precision)   |
| LOW  | 10 (wide, higher recall)      |

Retrieved rows include: question text, model answer, rubric, and cosine similarity score. The similarity score is included in the prompt so the LLM can judge how relevant each example is.

---

## Tier 3 — LLM Final Marker

| Path | Model | Notes |
|------|-------|-------|
| MID  | llama3 (Ollama, offline) | ~15–30s on CPU, ~3–5s on GPU |
| LOW  | Claude / GPT-4o (online) if `ONLINE_LLM_ENABLED=true`, else llama3 | Best quality for ambiguous answers |
| HIGH | — | LLM not called |

The LLM receives the full rubric-anchored prompt with retrieved context. It is asked to return structured JSON: `{mark, feedback, flagged, confidence}`. The mark is clamped to `max_marks`; the JSON is extracted with a regex that tolerates surrounding prose from less-compliant models.

---

## Question Generation — Two-Stage Pipeline

```
Stage 1: SLM (phi3:mini)
  Input:  source text (first 4,000 chars)
  Output: N pipe-separated skeletons: concept | one-line answer
  Cost:   ~2–5s per generation call

Stage 2: LLM (llama3)
  Input:  skeletons from stage 1
  Output: JSON array of full questions with rubrics, marks, tags
  Cost:   ~20–60s per batch of 30 skeletons
```

Benefits of two-stage vs single-stage:
- SLM extracts a broader, more diverse set of concepts from the source
- LLM focuses on enrichment (rubric writing, mark allocation) rather than reading comprehension
- Failures are isolated: if the SLM produces bad skeletons the pipeline falls back to single-stage LLM
- Parallelisable: skeletons can be batched for concurrent enrichment

---

## Data Model — New Columns

```sql
-- Added in migration 0002
ALTER TABLE submissions ADD COLUMN auto_confidence      FLOAT;
ALTER TABLE submissions ADD COLUMN marking_route        VARCHAR(10);  -- HIGH/MID/LOW
ALTER TABLE submissions ADD COLUMN slm_keyword_coverage FLOAT;
ALTER TABLE submissions ADD COLUMN slm_semantic_sim     FLOAT;
ALTER TABLE submissions ADD COLUMN slm_raw_score        FLOAT;
```

These columns feed the `/api/v1/analytics/` endpoints and allow continuous calibration of the confidence thresholds.

---

## Configuration Thresholds

```
CONFIDENCE_HIGH = 0.85    # ~40% of answers take HIGH path (no LLM)
CONFIDENCE_MID  = 0.55    # ~45% of answers take MID path (offline LLM)
# remainder     < 0.55    # ~15% take LOW path (online/offline + flag)
```

**Calibrating thresholds:**

After running the pipeline on a sample of submissions, compare `auto_mark` to `override_mark` for each route:

- If HIGH-path answers have a large `avg_override_delta`, lower `CONFIDENCE_HIGH`
- If MID-path answers have low flag rates and small deltas, you can raise `CONFIDENCE_HIGH`
- The Analytics dashboard shows these metrics per route in real time

---

## Performance Characteristics (CPU, no GPU)

| Route | Share | Time per answer | LLM calls |
|-------|-------|----------------|-----------|
| HIGH  | ~40%  | ~2s            | 0         |
| MID   | ~45%  | ~15–30s        | 1 offline |
| LOW   | ~15%  | ~5–60s         | 1 online/offline |

**Batch of 100 submissions (CPU, no GPU):**
- Without hybrid: 100 × llama3 = ~30–60 minutes
- With hybrid: 40 HIGH (2s) + 45 MID (20s) + 15 LOW (30s) ≈ **~25 minutes**
- With GPU (4090): HIGH ~2s, MID ~4s, LOW ~8s → **~7 minutes**
- With GPU + online fallback for LOW: **~5 minutes**

---

## Analytics API

```
GET /api/v1/analytics/pipeline               Route distribution + avg confidence/mark per tier
GET /api/v1/analytics/questions              Per-question flagged rate + override delta
GET /api/v1/analytics/confidence-distribution  Histogram (20 bins) for threshold calibration
```

---

## Switching / Upgrading Models

### Upgrade SLM to a better small model
```
# .env
SLM_MODEL_NAME=phi3:medium       # 14B, noticeably better, still fast on GPU
SLM_MODEL_NAME=mistral:7b-instruct  # strong alternative
```

### Upgrade LLM
```
LLM_MODEL_NAME=llama3:70b        # 70B, much better quality, needs ~40GB VRAM
LLM_MODEL_NAME=mistral:7b-instruct
```

### Enable online fallback for LOW path only
```
ONLINE_LLM_ENABLED=true
ONLINE_LLM_PROVIDER=anthropic
ONLINE_LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-...
```
Only ~15% of submissions take the LOW path, so API costs stay minimal even at scale.
