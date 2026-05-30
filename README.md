# QuizMark — AI-Powered Question Generator & Marker

A web app for instructors to upload a PDF textbook and automatically generate exam questions and mark student submissions using AI.

Upload a chapter → get a question bank with model answers and rubrics → use the marking tool to auto-grade student responses.

---

## Quick start (single command)

You need **Docker Desktop** and a **free Gemini API key** (get one at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)).

```bash
# Mac / Linux
bash setup.sh

# Windows
setup.bat
```

The script will:
1. Create a `.env` file with sensible defaults
2. Auto-generate a secure `SECRET_KEY`
3. Ask for your Gemini API key (required)
4. Ask you to choose an admin password
5. Build and start all services

Then open **http://localhost:3000** and log in with `admin` + the password you chose.

---

## What it does

- Upload a `.pdf` textbook — the app parses it into teaching chunks and embeds them in a vector store
- Generate questions (MCQ, short answer, true/false) from any book or chapter
- Each question comes with a model answer, a per-mark rubric, difficulty rating, and page references
- Submit student answers and get AI-powered marks with feedback
- RAG-backed marking — student responses are checked against the source textbook, not just the model answer

---

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 15, TypeScript, Tailwind CSS |
| Backend API | FastAPI (Python 3.11) |
| Database & vector store | MongoDB Atlas Local |
| AI (generation + marking + embeddings) | Google Gemini (gemini-2.5-flash, gemini-embedding-001) |
| Background jobs | Celery + Redis |
| Infrastructure | Docker Compose |

---

## Services after startup

| Service | URL |
|---|---|
| App | http://localhost:3000 |
| API docs | http://localhost:8000/docs |

---

## Useful commands

```bash
# Stop everything
docker compose down

# Restart without rebuilding
docker compose up -d

# Live logs
docker compose logs -f

# Rebuild after code changes
docker compose up -d --build

# Full reset — DELETES ALL DATA (books, questions, everything)
docker compose down -v

# Load demo student accounts
docker compose run --rm --no-deps backend python -m app.seed_demo_data
```

---

## How to generate questions

1. Log in as instructor at http://localhost:3000
2. Go to **Library** → upload a PDF textbook (up to 25 MB, up to 700 pages)
3. Wait for ingestion to complete (progress shown in real time)
4. Click the book → **Generate Questions**
5. Choose type (MCQ / short answer / true-false), difficulty, and count
6. Review and edit the generated questions in the Questions bank

---

## Configuration

All settings live in `.env` (created by `setup.sh` from `.env.example`). The only required value you need to supply is:

```env
GEMINI_API_KEY=your-key-here
```

Everything else has sensible defaults. See `.env.example` for the full list with explanations.

---

## Project structure

```
backend/app/
    api/v1/          — FastAPI route handlers
    services/
        pdf_service.py         — PDF parsing and chunking
        pdf_extractor.py       — vision-based chart/figure extraction
        question_generator.py  — LLM prompting and validation
        llm_service.py         — Gemini client
        rag_pipeline.py        — retrieval-augmented marking
        mongo_vector_store.py  — MongoDB vector search
    tasks/
        ingest_tasks.py        — background Celery job for large PDFs
        marking_tasks.py       — async marking pipeline

frontend/src/app/(instructor)/
    dashboard/       — overview and stats
    library/         — book management
    generate/        — question generation UI
```

---

## Known limitations

- Scanned / image-only PDFs won't work — needs a text-based PDF
- Gemini API key is required; no offline/local-only mode
