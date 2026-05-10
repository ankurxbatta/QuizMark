# Automated Web-Based Quiz Generation and Answer Evaluation System

An offline, privacy-first platform that uses a **local LLM (Ollama / llama3)** and a **RAG pipeline** to automatically generate statistics quiz questions from uploaded content — including **PDF textbooks** — collect student answers, and mark them using AI, with full instructor review, override, and audit capabilities.

> All AI inference runs on-premises via Ollama. No data ever leaves your infrastructure.

---

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Uploading a PDF Textbook](#uploading-a-pdf-textbook)
  - [Instructor Workflow](#instructor-workflow)
  - [Student Workflow](#student-workflow)
- [API Reference](#api-reference)
- [Architecture](#architecture)
- [Data Files](#data-files)
- [Security](#security)
- [Offline vs Online Models](#offline-vs-online-models)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)

---

## Features

### Question Generation
- Upload a **PDF textbook** or plain-text `.txt` file; the LLM generates **Short Answer, MCQ, or True/False** questions with model answers and rubrics automatically
- PDF text is extracted in-backend (pdfplumber + pypdf) — no external tools needed
- Batch generate up to **50 questions per upload** (configurable)
- Full CRUD management of the Q&A bank with topic tags and difficulty levels
- Seed bank of **200 pilot statistics questions** included in `data/questions_bank.json`
- Compatible with the **OpenStax Introductory Business Statistics** textbook (631 pages, included as sample)

### Auto-Marking (RAG Pipeline)
- Student answers are **embedded** (nomic-embed-text via Ollama) and matched against the vector store (pgvector)
- Top-K similar model answers are retrieved and used as context for the LLM marking prompt
- LLM returns a **structured JSON** response: `{mark, feedback, flagged}`
- Marks validated against `max_marks`, stored asynchronously via Celery + Redis
- Low-confidence responses automatically **flagged** for human review

### Instructor Dashboard
- Overview stats: total questions, pending marking, flagged submissions, last backup date
- Full Q&A bank manager: create, edit, delete questions
- Marking review queue with **override mark**, **override feedback**, and **override reason**
- Complete **audit log** for every override event
- CSV export for marks and audit log

### Student Portal
- Clean, mobile-friendly assessment interface
- Submit answers; marking happens in the background
- Confirmation screen on submission

### Security & Compliance
- **JWT authentication** (HS256, 30-minute expiry)
- **bcrypt** password hashing
- **Account lockout** after 3 failed attempts (5-minute cooldown)
- Full **audit trail** of all marking overrides
- UUID-based student identification (no PII in default schema)

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind CSS |
| Backend | FastAPI (Python 3.11), Pydantic v2, SQLAlchemy (async) |
| Database | PostgreSQL 16 + pgvector extension |
| LLM Inference | Ollama (llama3 for generation/marking, nomic-embed-text for embeddings) |
| PDF Extraction | pdfplumber + pypdf |
| Task Queue | Celery 5 + Redis 7 |
| Containerisation | Docker Compose |
| Migrations | Alembic |
| Auth | python-jose (JWT) + passlib (bcrypt) |

---

## Project Structure

```
automated_web_based_quiz_generation_and_answer_evaluation_system/
│
├── docker-compose.yml
├── .env.example
├── setup.sh / setup.bat
├── LICENSE
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/versions/0001_initial_schema.py
│   └── app/
│       ├── main.py
│       ├── core/
│       │   ├── config.py
│       │   ├── database.py
│       │   └── security.py
│       ├── models/models.py
│       ├── schemas/schemas.py
│       ├── services/
│       │   ├── llm_service.py          # Ollama adapter
│       │   ├── rag_pipeline.py         # RAG auto-marking
│       │   ├── question_generator.py   # LLM question generation
│       │   └── pdf_service.py          # PDF text extraction ← NEW
│       ├── tasks/
│       │   ├── celery_app.py
│       │   └── marking_tasks.py
│       └── api/v1/
│           ├── auth.py
│           ├── questions.py    # .pdf + .txt upload support ← UPDATED
│           ├── submissions.py
│           ├── marking.py
│           └── export.py
│
├── frontend/src/app/
│   ├── page.tsx                        # Login
│   ├── (instructor)/
│   │   ├── dashboard/page.tsx
│   │   ├── questions/page.tsx
│   │   ├── generate/page.tsx           # PDF + TXT upload UI ← UPDATED
│   │   ├── marking/page.tsx
│   │   └── export/page.tsx
│   └── (student)/assessment/page.tsx
│
├── data/
│   ├── questions_bank.json             # 200 pilot Q&A records
│   └── sample_submissions.csv          # 30 gold-marked submissions
│
├── scripts/generate_data.py
└── docs/
    ├── ARCHITECTURE.md
    ├── API.md
    └── MODEL_COMPARISON.md             # Offline vs Online guide ← NEW
```

---

## Prerequisites

| Requirement | Minimum Version | Notes |
|-------------|----------------|-------|
| Docker Desktop | 4.28+ | Allocate ≥ 8 GB RAM |
| Docker Compose | 2.24+ | Included with Docker Desktop |
| Free disk space | 10 GB+ | llama3 ≈ 4.7 GB, nomic-embed-text ≈ 274 MB |
| Python 3.11+ | Optional | Only for `scripts/generate_data.py` |

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/ankurbatta/automated_web_based_quiz_generation_and_answer_evaluation_system.git
cd automated_web_based_quiz_generation_and_answer_evaluation_system
```

### 2. Setup

**macOS / Linux:**
```bash
chmod +x setup.sh && ./setup.sh
```

**Windows:**
```bat
setup.bat
```

The script: copies `.env`, builds images, runs Alembic migrations, pulls LLM models, starts all six services.

### 3. Seed data (recommended)

```bash
python3 scripts/generate_data.py
```

Creates `data/questions_bank.json` (200 questions) and `data/sample_submissions.csv` (30 gold submissions).

### 4. Open

| Service | URL |
|---------|-----|
| Frontend | http://localhost:3000 |
| API docs (Swagger) | http://localhost:8000/docs |
| Ollama | http://localhost:11434 |

---

## Configuration

```bash
cp .env.example .env   # then edit SECRET_KEY and POSTGRES_PASSWORD
```

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | **Change this** — JWT signing key | — |
| `POSTGRES_PASSWORD` | **Change this** | — |
| `LLM_MODEL_NAME` | Ollama model for generation & marking | `llama3` |
| `EMBEDDING_MODEL` | Ollama model for embeddings | `nomic-embed-text` |
| `UPLOAD_MAX_SIZE_MB` | Max PDF/TXT upload size | `25` |
| `JWT_EXPIRY_MINUTES` | Token lifetime | `30` |
| `MAX_FAILED_LOGIN_ATTEMPTS` | Before lockout | `3` |
| `SIMILARITY_THRESHOLD` | Below this → flag for review | `0.75` |
| `TOP_K_RETRIEVAL` | RAG context window size | `5` |

---

## Usage

### Uploading a PDF Textbook

The platform accepts **PDF textbooks directly** — no manual conversion needed.

1. Go to **Dashboard → Upload Content & Generate Questions**
2. Click the upload zone and select your `.pdf` file (e.g. `IntroductoryBusinessStatistics-OP.pdf`)
3. The backend automatically:
   - Extracts text from up to **100 pages** using `pdfplumber`
   - Feeds the extracted content to the local LLM
   - Generates questions with model answers, rubrics, topic tags, and difficulty levels
4. Choose question type (Short Answer / MCQ / True-False) and quantity (1–50)
5. Click **Generate Questions**

**PDF requirements:**
- Must be a **text-based PDF** (not a scanned/image-only PDF)
- Maximum **25 MB** (configurable via `UPLOAD_MAX_SIZE_MB`)
- First 100 pages are used (sufficient for most textbook chapters)

**Tested with:** *Introductory Business Statistics* (OpenStax, 631 pages) — text extracts cleanly.

### Instructor Workflow

1. Log in → select **Instructor**
2. **Generate** questions from a PDF or TXT upload
3. **Manage Q&A bank** — review, edit, or delete generated questions
4. **Review Marking** — view auto-marks, override where needed with feedback and reason
5. **Export** — download marks CSV or audit log CSV

### Student Workflow

1. Log in → select **Student**
2. Answer questions in the assessment portal
3. Submit — marking runs in the background via the LLM pipeline

---

## API Reference

Full docs at http://localhost:8000/docs or [docs/API.md](docs/API.md).

### Key Endpoints

```
POST   /api/v1/auth/login
GET    /api/v1/questions/
POST   /api/v1/questions/                    Create question manually
POST   /api/v1/questions/generate            Generate from .pdf or .txt upload
PUT    /api/v1/questions/{id}
DELETE /api/v1/questions/{id}
POST   /api/v1/submissions/                  Submit answer → async marking
GET    /api/v1/submissions/
PUT    /api/v1/marking/{id}/override
GET    /api/v1/marking/flagged
GET    /api/v1/marking/audit-log
GET    /api/v1/export/marks                  CSV download
GET    /api/v1/export/audit                  CSV download
GET    /health
```

**Generate endpoint — accepted file types:**

| Type | Extension | Notes |
|------|-----------|-------|
| PDF textbook | `.pdf` | Text extracted automatically, up to 100 pages |
| Plain text | `.txt` | Used directly |

---

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full diagrams.

```
Browser ──► Next.js 14 (3000)
               │
               └──► FastAPI (8000)
                        ├──► PostgreSQL + pgvector (5432)
                        ├──► Celery Worker ──► Redis (6379)
                        └──► Ollama LLM (11434)
                                 ├── llama3
                                 └── nomic-embed-text
```

**PDF → Questions pipeline:**
```
Upload .pdf
    │
    ▼
pdf_service.py (pdfplumber → pypdf fallback)
    │  Extract text from up to 100 pages
    ▼
question_generator.py
    │  LLM prompt → structured JSON array
    ▼
Embed each Q+A (nomic-embed-text)
    │
    ▼
INSERT into questions table with pgvector embedding
```

---

## Data Files

| File | Description |
|------|-------------|
| `data/questions_bank.json` | 200 statistics Q&A records, 10 topics, 3 difficulties, 3 types |
| `data/sample_submissions.csv` | 30 synthetic gold-marked student submissions |

Regenerate: `python3 scripts/generate_data.py`

---

## Security

| Control | Implementation |
|---------|----------------|
| Password hashing | bcrypt (passlib) |
| Authentication | JWT HS256, 30-min expiry |
| Brute-force protection | 3-attempt lockout, 5-min cooldown |
| Audit trail | Every override logged with actor, delta, reason, timestamp |
| Data privacy | UUIDs for student IDs; all inference local |
| Offline inference | All LLM calls go to Ollama — zero external data transmission |

---

## Offline vs Online Models

See the full analysis in [docs/MODEL_COMPARISON.md](docs/MODEL_COMPARISON.md).

**Summary:**

| Factor | Offline (Ollama/llama3) | Online (GPT-4o, Claude) |
|--------|------------------------|------------------------|
| Data privacy | ✅ Complete — nothing leaves your server | ⚠️ Data sent to third-party API |
| Cost at scale | ✅ Zero per-call cost after hardware | ❌ Pay per token (can be significant) |
| Marking quality | ⚠️ Good for structured rubrics; weaker on nuance | ✅ Significantly better on complex, open-ended answers |
| Question generation | ⚠️ Adequate; occasional JSON formatting issues | ✅ More consistent, richer questions |
| Latency | ⚠️ 5–30s per marking call on CPU | ✅ 1–3s with API |
| Setup complexity | ⚠️ Requires Docker + 5–10 GB disk | ✅ Just an API key |
| Internet dependency | ✅ Fully offline | ❌ Requires internet |
| Compliance (FERPA/GDPR) | ✅ Inherently compliant | ⚠️ Requires DPA with vendor |

**Recommendation:** Start with **Ollama (offline)** for development, testing, and institutions with strict data governance requirements. Switch to **Claude or GPT-4o** for production marking if quality is the top priority and a data processing agreement is in place. The codebase is designed so switching only requires changing `LLM_MODEL_NAME` and pointing `OLLAMA_BASE_URL` to an OpenAI-compatible proxy.

---

## Development

```bash
# Backend only
cd backend && pip install -r requirements.txt
uvicorn app.main:app --reload

# Frontend only
cd frontend && npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev

# Celery worker
cd backend && celery -A app.tasks.celery_app worker --loglevel=info

# Migrations
cd backend && alembic upgrade head
```

---

## Troubleshooting

**PDF extraction returns empty text**
The PDF may be scanned (image-only). Open it in a PDF viewer — if you can't select text, it's a scan. Use a text-based PDF or OCR the document first.

**Ollama model not found**
```bash
docker compose exec llm ollama pull llama3
docker compose exec llm ollama pull nomic-embed-text
```

**Database connection refused**
```bash
docker compose logs db
# Check POSTGRES_USER / POSTGRES_PASSWORD match in .env
```

**Marking stuck as pending**
```bash
docker compose ps          # confirm worker is running
docker compose logs worker
```

**413 error on PDF upload**
Increase `UPLOAD_MAX_SIZE_MB` in `.env` (default 25 MB).

---

## Roadmap

- [ ] OCR fallback for scanned PDFs (pytesseract)
- [ ] Chapter-range selector for large PDF textbooks
- [ ] Role-based access control (RBAC) for multi-instructor deployments
- [ ] Student results portal (view own marks and feedback)
- [ ] Per-question analytics (difficulty calibration, discrimination index)
- [ ] PDF report export (per-student and cohort summary)
- [ ] OpenAI-compatible API mode (swap Ollama for GPT-4o / Claude)
- [ ] AWS Secrets Manager integration for production
- [ ] Kubernetes Helm chart for horizontal scaling
- [ ] Plagiarism / similarity detection across student answers

---

## License

MIT — see [LICENSE](LICENSE).

---

*Built as a capstone project demonstrating local LLM-powered educational tooling with full audit compliance.*
