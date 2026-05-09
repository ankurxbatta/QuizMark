# Architecture Overview

## System Context

```
Browser ──► Next.js 14 (port 3000)
               │
               └──► FastAPI Backend (port 8000)
                       │
                       ├──► PostgreSQL + pgvector (port 5432)
                       ├──► Redis broker (port 6379)
                       ├──► Celery Worker (async marking)
                       └──► Ollama LLM service (port 11434)
                                └── llama3 (generation + marking)
                                └── nomic-embed-text (embeddings)
```

## Service Roles

| Service | Image | Role |
|---------|-------|------|
| `frontend` | Node 20 / Next.js | Instructor & student UI |
| `backend` | Python 3.11 / FastAPI | REST API, orchestration |
| `db` | pgvector/pgvector:pg16 | Relational store + vector index |
| `llm` | ollama/ollama | Local LLM inference (fully offline) |
| `worker` | Same as backend | Celery async marking jobs |
| `broker` | Redis 7 | Job queue + result backend |

## Data Flow: Question Generation

```
Instructor uploads .txt file
        │
        ▼
POST /api/v1/questions/generate
        │
        ▼
question_generator.py  ──► Ollama /api/generate (llama3)
        │                        (structured JSON prompt)
        ▼
Parse JSON array of question objects
        │
        ▼
For each question: embed via Ollama /api/embeddings (nomic-embed-text)
        │
        ▼
INSERT into questions table (text + pgvector embedding)
```

## Data Flow: Answer Submission & Auto-Marking

```
Student submits answer
        │
        ▼
POST /api/v1/submissions/
        │
        ├──► INSERT Submission record (is_marked=false)
        │
        └──► Celery task: mark_submission_task.delay(submission_id)
                   │
                   ▼
         RAG Pipeline (rag_pipeline.py)
                   │
                   ├─ 1. Embed student answer (nomic-embed-text)
                   ├─ 2. pgvector similarity search → top-K model answers
                   ├─ 3. Build rubric-anchored prompt
                   ├─ 4. Ollama generate → {mark, feedback, flagged}
                   └─ 5. UPDATE Submission (auto_mark, auto_feedback, is_marked=true)
```

## Data Flow: Instructor Override

```
Instructor reviews flagged submission
        │
        ▼
PUT /api/v1/marking/{id}/override
        │
        ├──► UPDATE override_mark, override_feedback, override_reason
        ├──► SET is_flagged = false
        └──► INSERT AuditLog record
```

## Database Schema

```
users
  id uuid PK | username | hashed_password | role | failed_attempts | locked_until

questions
  id uuid PK | question_text | question_type | model_answer | rubric
  max_marks | topic_tag | difficulty | embedding vector(768) | created_at

submissions
  id uuid PK | student_id FK | question_id FK | answer_text
  auto_mark | auto_feedback | override_mark | override_feedback | override_reason
  is_flagged | is_marked | submitted_at | marked_at

audit_logs
  id uuid PK | event_type | actor_id | submission_id | detail | timestamp
```

## Security Design

| Concern | Mechanism |
|---------|-----------|
| Authentication | JWT (HS256), 30-min expiry |
| Password storage | bcrypt (passlib) |
| Brute-force | 3-attempt lockout, 5-min cooldown |
| Session timeout | JWT expiry + client-side cookie TTL |
| Data in transit | HTTPS via reverse proxy in production |
| Secrets management | `.env` locally; AWS Secrets Manager in production |
| Privacy | Student IDs stored as UUIDs; answers not exposed to other students |

## RAG Design

- **Embedding model**: `nomic-embed-text` (768-dim) via Ollama
- **Vector store**: pgvector with cosine distance index on `questions.embedding`
- **Retrieval**: cosine distance (`<=>` operator), top-K = 5
- **Confidence threshold**: submissions with similarity < 0.75 are flagged for human review
- **Prompt engineering**: rubric-anchored, structured JSON output enforced

## Celery Task Queue

- Broker: Redis (list-based)
- Result backend: Redis
- Worker concurrency: default (CPU core count)
- Retry policy: max 3 retries, 10-second back-off
- Task: `mark_submission_task` — wraps async RAG pipeline in sync event loop

## Scaling Considerations (Future)

- Replace Ollama with vLLM or AWS Bedrock for GPU-accelerated inference
- Add pgBouncer connection pooling for high-concurrency
- Kubernetes HPA for Celery workers
- AWS Secrets Manager + IAM roles for production secrets
