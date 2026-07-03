# QuizMark — AI-Powered Question Generator & Marker

An instructor tool to upload PDF textbooks, automatically generate exam questions using DeepSearch RAG, and auto-mark student submissions with AI feedback.

---

## Quick Start

You need **Docker Desktop**, a **Gemini API key** (free), an **OpenAI API key** (paid), and an **Anthropic API key** (paid).

```bash
# Mac / Linux
bash setup.sh

# Windows
setup.bat
```

The script auto-generates `SECRET_KEY`, prompts for your three API keys and an admin password, builds Docker images, and starts all services.

Open **http://localhost:3000** and log in with `admin` + the password you chose.

---

## What It Does

- **Upload any PDF textbook** — chapters, sections, tables, math formulas, and charts are extracted and embedded into MongoDB
- **Generate questions** (MCQ, short answer, true/false) using multi-round DeepSearch RAG: the system retrieves the most testable chunks, generates questions across all Bloom's taxonomy levels, then deduplicates
- **Auto-mark student answers** — MCQ and True/False are marked instantly against a structured answer key stored at generation time (no model call); short answers use RAG-backed LLM marking checked against the source textbook, with calculations re-verified rather than trusting the model answer blindly
- **Bundle questions into named quizzes** — group questions into a titled quiz and assign it to students as a unit; a student's assessment is the union of all their assigned quizzes (legacy per-question assignment still works)
- **Rendered math and figures** — question and answer text renders LaTeX with KaTeX, and questions can carry data-table or AI-generated figure assets
- **Resumable ingestion** — large PDFs (600+ pages) checkpoint every 6 pages; re-uploading the same PDF continues from where it stopped

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 15, TypeScript, Tailwind CSS (Plus Jakarta Sans, blue/green design system), KaTeX math rendering |
| Backend API | FastAPI (Python 3.11) |
| Ingestion pipeline | LangChain (LCEL chain: clean → chunk → validate → vision → embed) |
| Database + vector store | MongoDB Atlas Local (768-dim cosine vector search) |
| Background jobs | Celery + Redis (8 specialised workers) |
| Infrastructure | Docker Compose |
| AI — embeddings | Gemini `gemini-embedding-001` (768-dim, free) → OpenAI fallback |
| AI — vision / math | OpenAI `gpt-4o-mini` → Anthropic `claude-haiku` fallback |
| AI — generation | OpenAI `gpt-4o-mini` → Anthropic `claude-haiku` → Gemini fallback |
| AI — marking | OpenAI `gpt-4o-mini` → Anthropic `claude-haiku` → Gemini fallback |

---

## Services After Startup

| Service | URL | Purpose |
|---|---|---|
| App | http://localhost:3000 | Instructor + student UI |
| API docs | http://localhost:8000/docs | Interactive Swagger UI (disabled when `ENVIRONMENT=production`) |
| MongoDB UI | http://localhost:8081 | Browse the database |
| Flower | http://localhost:5555 | Live worker / task monitor |

---

## Architecture — 8 Specialised Workers

All workers run in **parallel**, each owning one Redis queue.

```
User → frontend → backend → Redis
                                ├── ingest_tasks    → worker-ingest     (PDF parse, resumable)
                                ├── vision_tasks    → worker-vision     (chart descriptions)
                                ├── math_tasks      → worker-math       (LaTeX extraction)
                                ├── clean_tasks     → worker-clean      (noise removal)
                                ├── embed_tasks     → worker-embed      (vector embeddings)
                                ├── deepsearch_tasks→ worker-deepsearch (multi-query RAG)
                                ├── gen_tasks       → worker-gen        (question generation)
                                └── mark_tasks      → worker-mark       (answer marking)
                                         │
                                      MongoDB
```

See `docs/ARCHITECTURE.md` for detailed flow diagrams.

---

## API Key Setup

Three keys are required. The system auto-rotates to the fallback when a provider hits its quota — no manual intervention needed.

| Provider | Role | Cost | Get Key |
|---|---|---|---|
| **Gemini** | Embeddings (768-dim) | Free quota | [aistudio.google.com](https://aistudio.google.com/app/apikey) |
| **OpenAI** | Vision · Math · Generation · Marking | Paid (~$0.20 per book) | [platform.openai.com](https://platform.openai.com/api-keys) |
| **Anthropic** | Fallback for all LLM tasks | Paid (standby) | [console.anthropic.com](https://console.anthropic.com) |

Typical cost to fully ingest a 600-page textbook: **~$0.22** (mostly vision/chart descriptions).

---

## Useful Commands

```bash
# Stop everything
docker compose down

# Restart without rebuilding
docker compose up -d

# Rebuild after code changes
docker compose up -d --build

# Live logs (all services)
docker compose logs -f

# Logs for a specific worker
docker compose logs -f worker-gen

# Full reset — DELETES ALL DATA (books, questions, everything)
docker compose down -v

# Check API key and provider health
curl -s http://localhost:8000/api/v1/admin/api-status \
  -H "Authorization: Bearer <your-token>"

# Trigger text cleaner on all stored chunks
curl -s -X POST http://localhost:8000/api/v1/admin/clean/all \
  -H "Authorization: Bearer <your-token>"
```

---

## How to Generate Questions

1. Log in as instructor at http://localhost:3000
2. **Add Book** → upload a PDF textbook (up to 25 MB, 700 pages)
3. Wait for ingestion to complete (progress shown live; large books resume automatically)
4. **Library** → click the book → **Generate Questions**
5. Choose type (MCQ / short answer / true/false), difficulty, and count per chapter
6. Review and edit questions in the Question Bank

---

## Project Structure

```
backend/app/
  api/v1/
    auth.py              — JWT login / logout
    questions.py         — book ingestion, question CRUD, job status
    submissions.py       — student answer submission
    marking.py           — trigger and retrieve marking results
    analytics.py         — instructor analytics
    admin.py             — API key health, text cleaner trigger
  services/
    llm_service.py       — OpenAI / Anthropic / Gemini clients + smart fallback
    api_key_manager.py   — quota tracking, auto-rotation between providers
    text_cleaner.py      — PDF noise removal (ligatures, mojibake, boilerplate)
    pdf_extractor.py     — PyMuPDF page extraction, chunk accumulator
    chunking.py          — recursive + semantic chunk splitting (LangChain)
    chunk_validator.py   — LLM math repair + dedup before DB insert
    ingestion_chain.py   — LCEL pipeline: clean → chunk → validate → vision → embed
    question_generator.py— DeepSearch retrieval, Bloom's taxonomy generation
    question_orchestrator.py — multi-round agentic question bank generation
    rag_pipeline.py      — hybrid pre-score + RAG marking pipeline
    mongo_vector_store.py— 768-dim vector search, checkpoint management
  tasks/
    celery_app.py        — 8 named queues + task routing
    ingest_tasks.py      — resumable PDF ingestion (page-by-page with checkpoints)
    clean_tasks.py       — async text cleaning tasks
    deepsearch_tasks.py  — multi-query RAG retrieval tasks
    marking_tasks.py     — async marking tasks

frontend/src/app/
  (instructor)/
    dashboard/           — stats overview
    generate/            — PDF upload + job progress
    library/             — book management, chapter browser
    questions/           — question bank
    marking/             — mark student submissions
    analytics/           — performance charts
  (student)/
    assessment/          — take an assessment

backend/tests/           — pytest suite (auth, RBAC, rate limiting, chunking, marking)

scripts/
  ingest_book.py         — standalone script to ingest a book directly (bypasses Celery)

docs/
  ARCHITECTURE.md        — system design, flow diagrams
  CONFIGURATION.md       — all .env variables explained
  GENERATION_PIPELINE.md — question generation deep-dive
  API.md                 — REST API reference

.github/workflows/ci.yml — CI: backend lint + tests, frontend build
```

---

## Development

Docker Compose automatically merges `docker-compose.override.yml`, which enables
backend hot-reload and mounts the source tree — so `docker compose up -d` gives you
a live-reloading dev environment, while the base `docker-compose.yml` stays production-safe.

Backend tests and linting run against Python 3.11:

```bash
cd backend
python3.11 -m venv .venv-test           # or: uv venv --python 3.11 .venv-test
.venv-test/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv-test/bin/pytest tests -q          # 29 tests, no DB or network needed
.venv-test/bin/ruff check app tests     # lint
```

Frontend:

```bash
cd frontend
npm install
npm run dev        # local dev server
npm run build      # production build (also run by CI)
```

CI (GitHub Actions) runs lint + tests + builds on every push to `main`/`Develop` and on PRs.

After changing `backend/requirements.txt` (e.g. the LangChain pins), rebuild images:
`docker compose build`.

---

## Known Limitations

- Scanned / image-only PDFs are not supported — requires a text-layer PDF
- All three API keys (Gemini, OpenAI, Anthropic) are required for full functionality
- The Book/ directory at the repo root is where the standalone ingest script looks for PDFs
