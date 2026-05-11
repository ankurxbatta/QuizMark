# PDF Quiz Question Generator

This is my final year project — a web app that lets instructors upload a PDF textbook and automatically generate exam questions from it using AI. It parses the PDF into sections, figures out which parts are actually teaching content, and uses an LLM (I'm using Claude but it works with GPT or Gemini too) to write proper questions with model answers and marking rubrics.

I built this because our department wastes a lot of time writing questions by hand from the same textbooks every year. The idea is that an instructor uploads a chapter, picks how many questions they want and what type (short answer, MCQ, or true/false), and gets back a question bank they can review and edit before using.

---

## What it does

- Upload a `.pdf` or `.txt` file
- Detects chapters and sections automatically from the PDF structure
- Filters out exercises, glossaries, and boilerplate — only keeps actual teaching content
- Sends content chunks to an LLM which returns questions in structured JSON
- Each question comes with a model answer, a rubric (one criterion per mark), difficulty level, and a reference back to which pages it came from
- Questions are saved to a database so you can browse, edit, and manage them

---

## Tech stack

I used:

- **Next.js 14** with TypeScript and Tailwind for the frontend
- **FastAPI** (Python 3.11) for the backend API
- **PostgreSQL** with the pgvector extension to store questions and their embeddings
- **Ollama** running locally for embeddings (nomic-embed-text) and as a fallback LLM
- **Claude / GPT-4o / Gemini** for the actual question generation (configurable)
- **Celery + Redis** for the async background jobs when processing large textbooks
- **Docker Compose** to run everything together
- **Alembic** for database migrations

---

## Running it locally

You need Docker Desktop (allocate at least 6 GB RAM) and an API key from one of the LLM providers.

```bash
git clone <repo-url>
cd marking-tools
cp .env.example .env
```

Open `.env` and fill in at minimum:

```env
SECRET_KEY=somethinglong
POSTGRES_PASSWORD=yourpassword
GENERATION_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

Then:

```bash
# Mac/Linux
chmod +x setup.sh && ./setup.sh

# Windows
setup.bat
```

Once everything is up, go to http://localhost:3000 and log in with the default admin credentials (set in `.env`).

---

## How to generate questions

1. Log in as instructor
2. Go to **Generate Questions**
3. Upload a PDF or text file
4. Optionally scan for chapters first so you can filter to one topic
5. Choose question type and how many you want (up to 50 at a time)
6. Hit Generate — takes around 15–30 seconds for a chapter
7. Review the results in the Questions bank and edit anything that needs fixing

For a full textbook I added an async mode that runs as a background job — you submit it and come back later to check progress.

---

## Project structure

The important bits:

```
backend/app/services/
    pdf_service.py          — PDF parsing and chunking
    question_generator.py   — chunk ranking, LLM prompting, validation
    llm_service.py          — provider clients (Anthropic, OpenAI, Gemini, Ollama)

backend/app/api/v1/
    questions.py            — all the API endpoints

backend/app/tasks/
    ingest_tasks.py         — background Celery job for large PDFs

frontend/src/app/(instructor)/
    generate/               — upload and generate UI
    questions/              — question bank browser
```

---

## Known issues / things I'd improve

- Scanned PDFs (image-based) don't work — you need a text-based PDF
- The chapter detection regex works on most textbooks I've tested but will probably miss some edge cases
- The fallback questions (when the LLM fails) are pretty basic — just recall-level
- No export to CSV or Word yet, that's on the to-do list

---

## Docs

More detail on how things work internally:

- [docs/GENERATION_PIPELINE.md](docs/GENERATION_PIPELINE.md) — how the PDF parsing and generation pipeline works
- [docs/API.md](docs/API.md) — API endpoints reference
- [docs/GENERATION_LLM.md](docs/GENERATION_LLM.md) — setting up and switching LLM providers
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — all the environment variables
