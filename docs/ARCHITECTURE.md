# System design notes

Just some notes on how I designed the overall architecture and why I made certain decisions.

---

## Why I split it into services the way I did

The backend has three main service files that handle the generation work:

**`pdf_service.py`** — only responsible for reading PDFs and turning them into chunks. It doesn't know anything about LLMs or questions. I kept it separate because PDF parsing is fiddly enough on its own and I wanted to be able to test it independently.

**`question_generator.py`** — handles chunk selection, ranking, prompting, and output validation. It calls the LLM service but doesn't care which provider is being used.

**`llm_service.py`** — all four provider clients (Anthropic, OpenAI, Gemini, Ollama) live here. They all implement the same `.generate()` interface so swapping providers is just a config change.

This separation made it easier to debug things — if generation is broken I can tell quickly whether it's a PDF parsing problem, a prompt problem, or an API problem.

---

## Why chunk-based rather than full-document

My first attempt just fed the whole PDF text into one prompt and asked for 20 questions. It kind of worked but had two big problems:

1. For a 600-page textbook you obviously can't fit it all in a context window
2. Even for shorter documents, all the questions ended up coming from the same section because that's where the most obvious content was

The chunk approach fixes both — you parse the whole document first, score each chunk, then spread questions across chapters using round-robin selection. The questions end up much more evenly distributed and the LLM gets focused context rather than a wall of text.

---

## The fallback chain

I spent a while getting the fallback logic right. The flow is:

1. Try the configured online LLM provider
2. If that fails or returns bad JSON, try the deterministic fallback generator
3. The fallback builds basic questions directly from the text without an LLM

The deterministic fallback is not great quality but it means generation never completely fails and returns zero questions. It's mostly there so the async ingest jobs don't silently fail on problematic chapters.

---

## Database choices

I used PostgreSQL mainly because it has the `pgvector` extension which lets you store and query embedding vectors. I'm storing an embedding for each question computed from the question text + model answer. Right now this is mostly forward-looking — the embeddings aren't used for generation itself, but they'd be useful for things like detecting duplicate questions across runs or doing semantic search over the question bank.

Alembic handles migrations. The main schema additions relevant to generation are `source_page_range` and `source_chunk` on the questions table, which give traceability back to where in the PDF a question came from.

---

## Async jobs

The async ingest uses Celery with Redis as the broker. I added this when I tested with a 631-page textbook and the synchronous endpoint was timing out. The async endpoint returns a job ID immediately and a Celery worker processes chapters in the background. The frontend polls the job status endpoint every few seconds to show progress.

It's a bit over-engineered for a project of this size but I wanted to understand how task queues work in practice.
