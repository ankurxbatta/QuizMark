# Environment variables

All config is in `.env` — copy `.env.example` to get started. Here's what everything does.

---

## The ones you actually need to change

| Variable | What it is |
|---|---|
| `SECRET_KEY` | Used to sign JWT tokens. Set this to something long and random. |
| `POSTGRES_PASSWORD` | Password for the database. Change this from the default. |
| `ADMIN_PASSWORD` | Password for the auto-created admin account. Required when `ADMIN_ENABLED=true`. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | Whichever API key matches your chosen LLM provider. |

---

## Database

| Variable | Example | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@db:5432/quizdb` | Full async connection string |
| `POSTGRES_USER` | `quizuser` | Used by the db Docker service |
| `POSTGRES_PASSWORD` | — | Change this |
| `POSTGRES_DB` | `quizdb` | Database name |

---

## Auth / login

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | — | Change this — used for JWT signing |
| `JWT_ALGORITHM` | `HS256` | Don't need to change this |
| `JWT_EXPIRY_MINUTES` | `30` | How long tokens stay valid |
| `MAX_FAILED_LOGIN_ATTEMPTS` | `3` | After this many wrong passwords, account locks |
| `LOCKOUT_DURATION_MINUTES` | `5` | How long the lockout lasts |
| `ADMIN_ENABLED` | `true` | Creates a default admin account on first startup |
| `ADMIN_USERNAME` | `admin` | Default login username |
| `ADMIN_PASSWORD` | — | Required when `ADMIN_ENABLED=true`; set a strong password |
| `ADMIN_ROLE` | `instructor` | Role assigned to the auto-created admin |

---

## Question generation LLM

| Variable | Default | Notes |
|---|---|---|
| `GENERATION_LLM_ENABLED` | `true` | Set to `false` to use local phi3:mini instead of an online provider |
| `GENERATION_LLM_PROVIDER` | `anthropic` | Options: `anthropic`, `openai`, `gemini` |
| `GENERATION_LLM_MODEL` | `claude-sonnet-4-20250514` | Model name for the chosen provider |
| `GENERATION_MAX_TOKENS` | `4096` | Max tokens in the LLM response — needs to be high enough for a full JSON array |
| `ANTHROPIC_API_KEY` | — | Required if provider is `anthropic` |
| `OPENAI_API_KEY` | — | Required if provider is `openai` |
| `GEMINI_API_KEY` | — | Required if provider is `gemini` |
| `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta` | Gemini API endpoint — shouldn't need to change this |

See [GENERATION_LLM.md](GENERATION_LLM.md) for more detail on picking a provider.

---

## PDF processing

| Variable | Default | Notes |
|---|---|---|
| `UPLOAD_MAX_SIZE_MB` | `25` | Max file size for uploads |
| `PDF_MAX_PAGES` | `620` | How many pages to read from a PDF — increase if your textbook is longer |
| `PDF_MIN_CHUNK_CHARS` | `300` | Chunks smaller than this get discarded (usually headers or short paragraphs) |
| `PDF_MAX_CHUNK_CHARS` | `3000` | Chunks larger than this get split at paragraph boundaries |

---

## Embeddings

| Variable | Default | Notes |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://llm:11434` | URL for the local Ollama service |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Embedding model to use — this needs to be pulled in Ollama |

If you get an error about the embedding model not being found:
```bash
docker compose exec llm ollama pull nomic-embed-text
```

---

## Celery / Redis

| Variable | Example |
|---|---|
| `CELERY_BROKER_URL` | `redis://broker:6379/0` |
| `CELERY_RESULT_BACKEND` | `redis://broker:6379/0` |

---

## Other

| Variable | Default | Notes |
|---|---|---|
| `BATCH_SIZE_LIMIT` | `50` | Max questions per generation request |
