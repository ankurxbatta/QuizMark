# Automated Web-Based Quiz Generation and Answer Evaluation System

An offline-first, privacy-safe platform powered by a **three-tier hybrid SLM + RAG + LLM pipeline**. Simple answers are marked by a small local model in ~2s; complex answers are escalated through RAG retrieval and a full LLM — with an optional online fallback only for genuinely ambiguous cases.

> ~40% of answers never touch the large LLM at all. All processing is on-premises by default.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Usage](#usage)
- [API Reference](#api-reference)
- [Analytics](#analytics)
- [Security](#security)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)

---

## Architecture Overview

```
Student answer
      │
      ▼
Tier 1  SLM pre-scorer (phi3:mini, ~2s)
        3 signals: keyword coverage · semantic similarity · SLM score
        → Confidence [0.0 – 1.0]
      │
      ▼
Confidence router
      │
  ┌───┴─────────────────────┐
  │                         │
≥0.85 HIGH            0.55–0.85 MID           <0.55 LOW
  │                         │                     │
SLM mark         RAG top-5  │           RAG wide top-10
accepted         + offline  │           + online LLM (opt.)
No LLM call        llama3   │           Auto-flagged
  │                         │                     │
  └─────────────────────────┴─────────────────────┘
                            │
                   mark + feedback + route + confidence
```

**Two-stage question generation:**
- Stage 1: SLM (phi3:mini) extracts concept skeletons from uploaded text/PDF
- Stage 2: LLM (llama3) enriches each skeleton into a full question with rubric, marks, and tags

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full details and performance numbers.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Next.js 14, TypeScript, Tailwind CSS |
| Backend | FastAPI (Python 3.11), Pydantic v2, SQLAlchemy async |
| Database | PostgreSQL 16 + pgvector |
| Tier-1 SLM | phi3:mini via Ollama (~2.3 GB, CPU-friendly) |
| Tier-3 Offline LLM | llama3 via Ollama |
| Tier-3 Online LLM | Claude / GPT-4o (optional, LOW path only) |
| Embeddings | nomic-embed-text via Ollama (768-dim) |
| PDF extraction | pdfplumber + pypdf |
| Task queue | Celery 5 + Redis 7 |
| Containerisation | Docker Compose |
| Migrations | Alembic |
| Auth | JWT (HS256) + bcrypt |

---

## Project Structure

```
├── docker-compose.yml
├── .env.example
├── setup.sh / setup.bat
│
├── backend/app/
│   ├── main.py
│   ├── core/
│   │   ├── config.py          # All settings incl. SLM + router thresholds
│   │   ├── database.py
│   │   └── security.py
│   ├── models/models.py       # Submission now stores route + SLM signals
│   ├── schemas/schemas.py
│   ├── services/
│   │   ├── llm_service.py     # OllamaClient · AnthropicClient · OpenAIClient
│   │   ├── slm_scorer.py      # Tier-1: keyword + semantic + SLM quick score
│   │   ├── rag_pipeline.py    # Hybrid router + RAG + Tier-3 LLM dispatch
│   │   ├── question_generator.py  # Two-stage: SLM skeletons → LLM enrichment
│   │   └── pdf_service.py     # PDF text extraction
│   ├── tasks/
│   │   ├── celery_app.py
│   │   └── marking_tasks.py
│   └── api/v1/
│       ├── auth.py
│       ├── questions.py
│       ├── submissions.py
│       ├── marking.py
│       ├── export.py
│       └── analytics.py       # Pipeline analytics (NEW)
│
├── frontend/src/app/
│   ├── page.tsx               # Login
│   ├── (instructor)/
│   │   ├── dashboard/         # Dashboard with pipeline explainer
│   │   ├── questions/         # Q&A bank CRUD
│   │   ├── generate/          # PDF/TXT upload + generation
│   │   ├── marking/           # Review + override
│   │   ├── analytics/         # Route distribution, confidence histogram (NEW)
│   │   └── export/
│   └── (student)/assessment/
│
├── data/
│   ├── questions_bank.json    # 200 pilot statistics questions
│   └── sample_submissions.csv # 30 gold-marked submissions
│
├── scripts/generate_data.py
└── docs/
    ├── ARCHITECTURE.md        # Full hybrid pipeline design
    ├── API.md
    └── MODEL_COMPARISON.md
```

---

## Prerequisites

| Requirement | Minimum | Notes |
|-------------|---------|-------|
| Docker Desktop | 4.28+ | Allocate ≥ 10 GB RAM |
| Docker Compose | 2.24+ | Included with Docker Desktop |
| Free disk space | ~12 GB | llama3 (4.7 GB) + phi3:mini (2.3 GB) + nomic-embed-text (274 MB) |
| Python 3.11+ | Optional | Only for `scripts/generate_data.py` |

---

## Quick Start

```bash
git clone https://github.com/ankurbatta/automated_web_based_quiz_generation_and_answer_evaluation_system.git
cd automated_web_based_quiz_generation_and_answer_evaluation_system
```

**macOS / Linux:**
```bash
chmod +x setup.sh && ./setup.sh
```

**Windows:**
```bat
setup.bat
```

The setup script pulls and starts all services, runs DB migrations, and pulls three Ollama models:
- `phi3:mini` — Tier-1 SLM pre-scorer
- `llama3` — Tier-3 offline LLM marker
- `nomic-embed-text` — embeddings for RAG and semantic similarity

Then seed the Q&A bank:
```bash
python3 scripts/generate_data.py
```

Open **http://localhost:3000**.

---

## Configuration

```bash
cp .env.example .env   # edit SECRET_KEY and POSTGRES_PASSWORD
```

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | **Change this** — JWT signing key | — |
| `POSTGRES_PASSWORD` | **Change this** | — |
| `SLM_MODEL_NAME` | Tier-1 pre-scorer | `phi3:mini` |
| `LLM_MODEL_NAME` | Tier-3 offline marker | `llama3` |
| `CONFIDENCE_HIGH` | Above this → accept SLM mark | `0.85` |
| `CONFIDENCE_MID` | Above this → offline LLM path | `0.55` |
| `ONLINE_LLM_ENABLED` | Enable online fallback for LOW path | `false` |
| `ONLINE_LLM_PROVIDER` | `anthropic` or `openai` | `anthropic` |
| `ANTHROPIC_API_KEY` | Required if online enabled | — |
| `TOP_K_RETRIEVAL` | RAG context size (MID path) | `5` |
| `TOP_K_WIDE_RETRIEVAL` | RAG context size (LOW path) | `10` |
| `UPLOAD_MAX_SIZE_MB` | Max PDF/TXT upload | `25` |

---

## Usage

### Uploading a PDF

1. Dashboard → **Upload Content & Generate Questions**
2. Upload `.pdf` or `.txt` (e.g. `IntroductoryBusinessStatistics-OP.pdf`)
3. Select question type and count (1–50)
4. Click **Generate Questions**

The pipeline: SLM extracts concept skeletons → LLM enriches into full questions with rubrics.

### Instructor workflow

1. Log in → Instructor
2. Generate questions from PDF/text
3. Review and edit the Q&A bank
4. Monitor marking queue — override flagged answers
5. Check **Pipeline Analytics** to calibrate confidence thresholds
6. Export marks CSV

### Student workflow

1. Log in → Student
2. Complete assessment
3. Submit — pipeline marks asynchronously

---

## API Reference

Full interactive docs: http://localhost:8000/docs · Markdown: [docs/API.md](docs/API.md)

### Hybrid pipeline endpoints

```
POST /api/v1/submissions/                Submit answer → triggers hybrid pipeline
GET  /api/v1/analytics/pipeline          Route distribution + avg confidence per tier
GET  /api/v1/analytics/questions         Per-question flagged rate + override delta
GET  /api/v1/analytics/confidence-distribution  Histogram for threshold calibration
```

Submission response now includes:
```json
{
  "mark": 3.5,
  "feedback": "[Route:MID|Conf:0.71] Good understanding...",
  "flagged": false,
  "route": "MID",
  "confidence": 0.71
}
```

---

## Analytics

The Analytics dashboard at `/instructor/analytics` shows:

- **Route distribution** — what % of answers went HIGH / MID / LOW
- **Per-route averages** — confidence, mark, keyword coverage, semantic similarity, flagged count, override count
- **Confidence histogram** — 20-bin chart with threshold markers; useful for tuning `CONFIDENCE_HIGH` and `CONFIDENCE_MID`
- **Per-question table** — avg confidence, flagged rate, override delta per question; large override deltas indicate rubric issues

---

## Security

| Control | Implementation |
|---------|----------------|
| Password hashing | bcrypt (passlib) |
| Authentication | JWT HS256, 30-min expiry |
| Brute-force protection | 3-attempt lockout, 5-min cooldown |
| Audit trail | Every override logged with actor, delta, reason, timestamp |
| Data privacy | UUIDs for student IDs; all inference local by default |
| Online fallback | Only ~15% of submissions, only if explicitly enabled |

---

## Development

```bash
# Backend
cd backend && pip install -r requirements.txt
uvicorn app.main:app --reload

# Frontend
cd frontend && npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev

# Celery worker
cd backend && celery -A app.tasks.celery_app worker --loglevel=info

# New migration after model changes
cd backend && alembic revision --autogenerate -m "description"
alembic upgrade head
```

---

## Troubleshooting

**phi3:mini not found**
```bash
docker compose exec llm ollama pull phi3:mini
```

**SLM returns no skeletons during generation**
Normal on very short or poorly-structured text. The pipeline automatically falls back to single-stage LLM generation.

**All answers routed to LOW**
Check that `nomic-embed-text` is pulled and that `questions.embedding` is populated. Run:
```bash
docker compose exec llm ollama pull nomic-embed-text
```

**Confidence always ~0.5**
The SLM score is falling back. Check `docker compose logs worker` — Ollama may be returning errors for `phi3:mini`.

**Online LLM not triggering**
Ensure `ONLINE_LLM_ENABLED=true` and the relevant API key is set in `.env`.

---

## Roadmap

- [ ] Fine-tune SLM on historically-marked submissions for domain-specific accuracy
- [ ] Confidence threshold auto-calibration using override feedback loop
- [ ] OCR fallback for scanned PDFs (pytesseract)
- [ ] Chapter-range selector for large textbooks
- [ ] Batch CSV submission import
- [ ] Student results portal
- [ ] PDF report export (per-student and cohort)
- [ ] Kubernetes Helm chart with HPA for Celery workers
- [ ] vLLM backend support for GPU-accelerated inference

---

## License

MIT — see [LICENSE](LICENSE).
