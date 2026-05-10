# Offline vs Online LLM: Which is Better for This System?

This document gives a detailed, honest comparison of running the quiz generation and auto-marking pipeline with a **local offline model** (Ollama / llama3) versus an **online API model** (e.g. Claude 3.5 Sonnet, GPT-4o).

---

## 1. The Core Trade-off

```
Offline model                        Online model
─────────────────────────────────    ─────────────────────────────────
Privacy & compliance: excellent      Quality & consistency: excellent
Cost at scale: ~zero                 Setup simplicity: excellent
Internet dependency: none            Latency: excellent
Quality: good (with prompt eng.)     Privacy: requires vendor DPA
Latency on CPU: slow (5–30s)         Cost at scale: can be significant
```

Neither is universally better. The right choice depends on your institution's constraints.

---

## 2. Detailed Factor Comparison

### 2.1 Data Privacy & Compliance

| | Offline | Online |
|---|---|---|
| Student answers leave your server? | **Never** | Yes — sent to vendor API |
| FERPA compliance (US) | Inherently compliant | Requires a signed FERPA MOU with vendor |
| GDPR compliance (EU) | Inherently compliant | Requires a Data Processing Agreement (DPA) |
| Institutional IT approval needed? | Typically low friction | Often requires formal vendor review |

**Verdict: Offline wins decisively** for any institution that handles student data under FERPA, GDPR, or similar regulations — unless you can secure a formal data processing agreement with the API vendor.

---

### 2.2 Marking Quality

This is where online models have a clear edge:

| Task | Offline (llama3 7B) | Online (Claude 3.5 / GPT-4o) |
|------|--------------------|-----------------------------|
| Short factual answers | Good | Excellent |
| Rubric-guided marking | Good with structured prompts | Excellent, consistent |
| Partial credit detection | Moderate — misses nuance | Strong |
| Open-ended / analytical answers | Weak — tends to over/under-mark | Strong |
| JSON output reliability | Moderate — occasional format breaks | Excellent |
| Marking consistency (same answer twice) | Moderate variance | Low variance |

**Root cause:** llama3 7B has about 7 billion parameters. GPT-4o and Claude 3.5 Sonnet are estimated at 200B+ parameters with RLHF fine-tuning specifically for instruction-following. Larger models are simply better at interpreting rubrics and detecting partial credit in nuanced natural-language answers.

**Mitigation for offline:** The RAG pipeline in this system partially compensates — by providing the model with similar worked examples during marking, llama3 performs noticeably better than it would with a bare prompt. The flagging mechanism then catches low-confidence marks for human review.

---

### 2.3 Question Generation Quality

| Task | Offline (llama3) | Online |
|------|-----------------|--------|
| Well-formed questions | Good | Excellent |
| Rubric detail | Adequate | Rich and detailed |
| Consistent JSON structure | Occasional failures | Very consistent |
| Topic coverage breadth | Good | Excellent |
| Avoids trivial / repeated questions | Moderate | Strong |

**Practical impact:** With llama3, expect to manually review and edit ~20–30% of generated questions. With an online model, this drops to ~5–10%.

---

### 2.4 Cost

| | Offline | Online |
|---|---|---|
| Per-call cost | £0 / $0 | ~$0.002–0.015 per marking call (varies by model + answer length) |
| Hardware cost | One-time: GPU or CPU server | None |
| 1,000 submissions | ~£0 | ~$2–15 |
| 10,000 submissions | ~£0 | ~$20–150 |
| 100,000 submissions | ~£0 | ~$200–1,500 |

**Verdict:** At small scale (< 5,000 submissions/year), online API costs are modest. At institutional scale, offline wins on cost.

---

### 2.5 Latency

| | Offline (CPU) | Offline (GPU) | Online |
|---|---|---|---|
| Per marking call | 10–60 seconds | 2–8 seconds | 1–3 seconds |
| Batch of 30 submissions | 5–30 minutes | 1–4 minutes | 30–90 seconds |
| Impact on UX | Marking always async (Celery) | Marking always async | Could be near-real-time |

**Note:** Because this system already uses Celery for async marking, the latency difference is largely invisible to students — they submit and get notified later regardless. It matters more for instructor turnaround time.

---

### 2.6 Setup & Operational Complexity

| | Offline | Online |
|---|---|---|
| Setup | Docker + 5–10 GB disk download | API key, one env variable |
| Maintenance | Manage Ollama, model updates | None |
| Downtime risk | Local hardware failure | Vendor outage (rare, usually <0.1%) |
| Model updates | Manual pull | Automatic |

---

## 3. Switching Between Models

This codebase is designed to make switching straightforward. The `LLMService` class in `backend/app/services/llm_service.py` is the only file that needs to change.

### Option A: Keep Ollama, upgrade the model

```bash
# In .env — use a larger / better local model
LLM_MODEL_NAME=llama3:70b          # much better quality, needs ~40 GB VRAM
LLM_MODEL_NAME=mistral:7b-instruct # good alternative to llama3
LLM_MODEL_NAME=phi3:14b            # strong for instruction-following
```

Pull the new model:
```bash
docker compose exec llm ollama pull llama3:70b
```

### Option B: Switch to an OpenAI-compatible online API

Replace the `generate()` and `embed()` methods in `llm_service.py`:

```python
# backend/app/services/llm_service.py  (online variant)
import httpx
from app.core.config import settings

class LLMService:
    async def generate(self, prompt: str) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": settings.LLM_TEMPERATURE,
                },
                timeout=60,
            )
            return resp.json()["choices"][0]["message"]["content"]

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={"model": "text-embedding-3-small", "input": text},
            )
            return resp.json()["data"][0]["embedding"]
```

Add to `.env`:
```
OPENAI_API_KEY=sk-...
```

### Option C: Use the Anthropic API (Claude)

```python
    async def generate(self, prompt: str) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            return resp.json()["content"][0]["text"]
```

---

## 4. Recommended Strategy

### Stage 1 — Development & Piloting (now)
**Use Ollama (offline) with llama3.**
- Zero cost, full privacy, no vendor dependency
- Excellent for building and testing the pipeline
- Use the flagging mechanism to catch weak auto-marks
- Target: flag rate ~20–30%, review all flagged submissions manually

### Stage 2 — Quality Improvement (without going online)
**Upgrade to llama3:70b or mistral:instruct on a GPU server.**
- Flag rate drops to ~10–15%
- Still fully offline and compliant
- Costs: one GPU server (~£2–5K one-time or cloud GPU instance)

### Stage 3 — Production at Scale (if quality is paramount)
**Switch `llm_service.py` to Claude or GPT-4o API.**
- Flag rate drops to ~3–5%
- Near-real-time marking
- Requires: API key + signed DPA with Anthropic/OpenAI
- At 10,000 submissions/year: ~$50–150/year — negligible

### Institutions with strict data governance
**Stay on Stage 1 or 2 permanently.**
- FERPA / GDPR compliance is automatic
- Pair with strong rubrics and the instructor override workflow
- Consider fine-tuning llama3 on your own historically-marked submissions (future roadmap item)

---

## 5. Summary Table

| Criterion | Weight for most HE institutions | Offline winner? |
|-----------|--------------------------------|-----------------|
| Student data privacy | **Very high** | ✅ Offline |
| Regulatory compliance (FERPA/GDPR) | **Very high** | ✅ Offline |
| Marking quality | High | ❌ Online |
| Cost at scale | Medium | ✅ Offline |
| Latency | Low (async system) | ❌ Online |
| Setup simplicity | Low | ❌ Online |
| No internet dependency | Medium | ✅ Offline |

**For an educational institution: start offline, move online only if you can satisfy compliance requirements and quality justifies the cost.**
