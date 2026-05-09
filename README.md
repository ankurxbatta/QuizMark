# Automated Web-Based Quiz Generation and Answer Evaluation System

An offline, privacy-first platform that uses a **local LLM (Ollama / llama3)** and a **RAG pipeline** to automatically generate statistics quiz questions from uploaded content, collect student answers, and mark them using AI — with full instructor review, override, and audit capabilities.

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
- [API Reference](#api-reference)
- [Architecture](#architecture)
- [Data Files](#data-files)
- [Security](#security)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)

---

## Features

### Question Generation
- Upload a plain-text content file; the LLM generates **Short Answer, MCQ, or True/False** questions with model answers and rubrics in one step
- Batch generate up to **50 questions per upload** (configurable)
- Full CRUD management of the Q&A bank with topic tags and difficulty levels
- Seed bank of **200 pilot statistics questions** included in `data/questions_bank.json`

### Auto-Marking (RAG Pipeline)
- Student answers are **embedded** (nomic-embed-text via Ollama) and matched against the vector store (pgvector)
- Top-K similar model answers are retrieved and used as context for the LLM marking prompt
- LLM returns a **structured JSON** response: `{mark, feedback, flagged}`
- Marks are validated against `max_marks` and stored asynchronously (Celery + Redis)
- Low-confidence responses are automatically **flagged** for human review

### Instructor Dashboard
- Overview stats: total questions, pending marking, flagged submissions, last backup date
- Full Q&A bank manager: create, edit, delete questions
- Marking review queue with **flag/unflag**, **override mark**, **override feedback**, and **override reason**
- Complete **audit log** for every override event
- CSV export for marks and audit log

### Student Portal
- Clean, mobile-friendly assessment interface
- Loads questions from the live Q&A bank
- Submit answers; receive confirmation; marking runs in the background

### Security & Compliance
- **JWT authentication** (HS256, 30-minute expiry)
- **bcrypt** password hashing
- **Account lockout** after 3 failed attempts (5-minute cooldown)
- Session timeout enforcement
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
| Task Queue | Celery 5 + Redis 7 |
| Containerisation | Docker Compose |
| Migrations | Alembic |
| Auth | python-jose (JWT) + passlib (bcrypt) |

---

## Project Structure

```
automated_web_based_quiz_generation_and_answer_evaluation_system/
│
├── docker-compose.yml          # Orchestrates all 6 services
├── .env.example                # Environment variable template
├── setup.sh / setup.bat        # First-run initialisation scripts
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/
│   │       └── 0001_initial_schema.py
│   └── app/
│       ├── main.py             # FastAPI application entry point
│       ├── core/
│       │   ├── config.py       # Pydantic settings (reads .env)
│       │   ├── database.py     # Async SQLAlchemy engine + session
│       │   └── security.py     # JWT creation/validation, bcrypt hashing
│       ├── models/
│       │   └── models.py       # SQLAlchemy ORM: User, Question, Submission, AuditLog
│       ├── schemas/
│       │   └── schemas.py      # Pydantic request/response models
│       ├── services/
│       │   ├── llm_service.py       # Ollama HTTP adapter (generate + embed)
│       │   ├── rag_pipeline.py      # RAG marking pipeline
│       │   └── question_generator.py # LLM question generation from text
│       ├── tasks/
│       │   ├── celery_app.py        # Celery configuration
│       │   └── marking_tasks.py     # Async marking Celery task
│       └── api/v1/
│           ├── auth.py         # POST /auth/login
│           ├── questions.py    # GET/POST/PUT/DELETE /questions/ + /generate
│           ├── submissions.py  # POST/GET /submissions/
│           ├── marking.py      # PUT /marking/{id}/override + audit log
│           └── export.py       # GET /export/marks + /export/audit (CSV)
│
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   ├── next.config.js
│   ├── tailwind.config.ts
│   └── src/
│       ├── app/
│       │   ├── layout.tsx           # Root layout
│       │   ├── globals.css
│       │   ├── page.tsx             # Login page (role selector)
│       │   ├── (instructor)/
│       │   │   ├── dashboard/       # Stats overview + quick actions
│       │   │   ├── questions/       # Q&A bank CRUD
│       │   │   ├── generate/        # Upload content → generate questions
│       │   │   ├── marking/         # Review submissions + override marks
│       │   │   └── export/          # Download CSV reports
│       │   └── (student)/
│       │       └── assessment/      # Student quiz submission portal
│       └── lib/
│           └── api.ts               # Axios instance with JWT interceptor
│
├── data/
│   ├── questions_bank.json     # 200 pilot statistics Q&A (run scripts/generate_data.py)
│   └── sample_submissions.csv  # 30 gold-marked student submissions for testing
│
├── scripts/
│   └── generate_data.py        # Generates data/ files (run once)
│
└── docs/
    ├── ARCHITECTURE.md         # System architecture and data flow diagrams
    └── API.md                  # Full REST API reference
```

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Docker Desktop | ≥ 4.28 | Ensure ≥ 8 GB RAM allocated |
| Docker Compose | ≥ 2.24 | Included with Docker Desktop |
| Python 3.11+ | Optional | Only needed to run `scripts/generate_data.py` locally |
| Free disk space | ≥ 10 GB | Ollama model weights (llama3 ≈ 4.7 GB, nomic-embed-text ≈ 274 MB) |

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/ankurbatta/automated_web_based_quiz_generation_and_answer_evaluation_system.git
cd automated_web_based_quiz_generation_and_answer_evaluation_system
```

### 2. Run the setup script

**macOS / Linux:**
```bash
chmod +x setup.sh
./setup.sh
```

**Windows (PowerShell or CMD):**
```bat
setup.bat
```

The setup script will:
1. Copy `.env.example` → `.env` (edit secrets before continuing)
2. Pull and build all Docker images
3. Run Alembic database migrations
4. Pull the LLM and embedding models via Ollama
5. Start all six services

### 3. Seed the data (optional but recommended)

```bash
python3 scripts/generate_data.py
```

This writes `data/questions_bank.json` (200 questions) and `data/sample_submissions.csv` (30 gold submissions). Import questions via the instructor UI or POST to `/api/v1/questions/`.

### 4. Open the application

| Service | URL |
|---------|-----|
| Frontend (UI) | http://localhost:3000 |
| Backend API docs | http://localhost:8000/docs |
| Ollama | http://localhost:11434 |

---

## Configuration

Copy `.env.example` to `.env` and edit before first run:

```bash
cp .env.example .env
```

Key variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | **Change this** — JWT signing key (min 32 chars) | — |
| `POSTGRES_PASSWORD` | **Change this** — database password | — |
| `LLM_MODEL_NAME` | Ollama model for generation and marking | `llama3` |
| `EMBEDDING_MODEL` | Ollama model for embeddings | `nomic-embed-text` |
| `JWT_EXPIRY_MINUTES` | Token lifetime in minutes | `30` |
| `MAX_FAILED_LOGIN_ATTEMPTS` | Before account lockout | `3` |
| `BATCH_SIZE_LIMIT` | Max questions generated per upload | `50` |
| `SIMILARITY_THRESHOLD` | Below this cosine similarity → flag for review | `0.75` |
| `TOP_K_RETRIEVAL` | Number of similar Q&As retrieved for RAG context | `5` |

---

## Usage

### Instructor Workflow

1. **Log in** at http://localhost:3000 → select **Instructor** → enter credentials
2. **Generate questions**: Dashboard → *Upload Content & Generate Questions* → upload a `.txt` file, select type and count → click Generate
3. **Manage Q&A bank**: Dashboard → *Manage Q&A Bank* → create, edit, or delete questions manually
4. **Review marking**: Dashboard → *Review & Mark Submissions* → view auto-marks, override where needed, provide feedback and reason
5. **Export results**: Dashboard → *Export Results* → download marks CSV or audit log CSV

### Student Workflow

1. **Log in** at http://localhost:3000 → select **Student** → enter credentials
2. Questions load automatically from the live Q&A bank
3. Write answers in each text area and click **Submit Assessment**
4. Marking runs asynchronously; results visible to instructor once complete

---

## API Reference

Full documentation: http://localhost:8000/docs (Swagger UI) or [docs/API.md](docs/API.md)

### Key Endpoints

```
POST   /api/v1/auth/login                  Authenticate → JWT
GET    /api/v1/questions/                  List questions (filter by topic, difficulty)
POST   /api/v1/questions/                  Create question
POST   /api/v1/questions/generate          Generate from text file upload
PUT    /api/v1/questions/{id}              Update question
DELETE /api/v1/questions/{id}              Delete question
POST   /api/v1/submissions/                Submit answer (triggers async marking)
GET    /api/v1/submissions/                List all submissions
PUT    /api/v1/marking/{id}/override       Override auto-mark
GET    /api/v1/marking/flagged             List flagged submissions
GET    /api/v1/marking/audit-log           Full audit trail (JSON)
GET    /api/v1/export/marks                Download marks CSV
GET    /api/v1/export/audit                Download audit log CSV
GET    /health                             Service health check
```

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
                                 ├── llama3 (generation & marking)
                                 └── nomic-embed-text (embeddings)
```

**RAG Marking Pipeline:**
1. Embed the student answer using `nomic-embed-text`
2. Retrieve top-K most similar question+model-answer pairs from pgvector
3. Construct a rubric-anchored LLM prompt with retrieved context
4. Call `llama3` → parse structured JSON: `{mark, feedback, flagged}`
5. Persist results; auto-flag low-confidence responses

---

## Data Files

| File | Description |
|------|-------------|
| `data/questions_bank.json` | 200 statistics Q&A records across 10 topics, 3 difficulties, 3 question types |
| `data/sample_submissions.csv` | 30 synthetic student submissions with gold marks (for evaluation/testing) |

Regenerate:
```bash
python3 scripts/generate_data.py
```

---

## Security

| Control | Implementation |
|---------|----------------|
| Password hashing | bcrypt via passlib |
| Token authentication | JWT (HS256), 30-min expiry |
| Brute-force protection | 3-attempt lockout, 5-min cooldown |
| Audit trail | Every mark override logged with actor, change, reason, timestamp |
| Data privacy | Student IDs as UUIDs; no PII in default schema |
| Offline inference | All LLM calls go to local Ollama — no external API keys |

---

## Development

### Run backend locally

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Run frontend locally

```bash
cd frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

### Run Celery worker locally

```bash
cd backend
celery -A app.tasks.celery_app worker --loglevel=info
```

### Apply database migrations

```bash
cd backend
alembic upgrade head

# After model changes:
alembic revision --autogenerate -m "your description"
```

### Rebuild a single service

```bash
docker compose up --build backend
```

---

## Troubleshooting

**Ollama model not found**
```bash
docker compose exec llm ollama pull llama3
docker compose exec llm ollama pull nomic-embed-text
```

**Database connection refused**
```bash
docker compose logs db
# Check POSTGRES_USER / POSTGRES_PASSWORD in .env match the db service environment
```

**Celery worker not picking up tasks**
```bash
docker compose logs worker
# Ensure CELERY_BROKER_URL=redis://broker:6379/0 in .env
```

**Frontend cannot reach backend**
- In Docker: `NEXT_PUBLIC_API_URL=http://backend:8000`
- Local dev: `NEXT_PUBLIC_API_URL=http://localhost:8000`

**Marking stuck as pending**
```bash
docker compose ps        # verify worker is running
docker compose logs worker
```

---

## Roadmap

- [ ] Role-based access control (RBAC) for multi-instructor deployments
- [ ] Batch submission upload (CSV import) for large cohorts
- [ ] Student results portal (view own marks and feedback)
- [ ] Per-question analytics (difficulty calibration, discrimination index)
- [ ] Configurable rubric editor with drag-and-drop criteria
- [ ] PDF report export (per-student and cohort summary)
- [ ] AWS Secrets Manager integration for production deployments
- [ ] Kubernetes Helm chart for horizontal scaling
- [ ] Support for additional LLM backends (vLLM, AWS Bedrock, OpenAI-compatible)
- [ ] Plagiarism / similarity detection across student answers

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

*Built as a capstone project demonstrating local LLM-powered educational tooling with full audit compliance.*
