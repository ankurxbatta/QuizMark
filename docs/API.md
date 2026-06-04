# API Reference

Base URL: `http://localhost:8000/api/v1`

All protected endpoints require a Bearer token. Get one from `POST /auth/login`.

Interactive docs with live testing: **http://localhost:8000/docs**

---

## Auth

### `POST /auth/login`
```json
{ "username": "admin", "password": "your-password" }
```
Returns `{ "access_token": "...", "token_type": "bearer" }`.

---

## Books and Ingestion

### `POST /questions/ingest-book`
Upload a PDF for ingestion. Returns immediately with a `job_id`.
- Form field: `file` (PDF, max 25 MB)
- If the same PDF was uploaded before, returns `resumed: true` and continues from the last checkpoint

```json
{
  "job_id": "abc123",
  "status": "queued",
  "resumed": false
}
```

### `GET /questions/jobs/{job_id}`
Poll ingestion job status.
```json
{
  "job_id": "abc123",
  "status": "processing",
  "total_pages": 631,
  "pages_done": 200,
  "progress_percent": 31,
  "progress_message": "Read 200/631 pages · 87 chunks stored",
  "chunks_stored": 87
}
```
Status values: `queued` · `processing` · `done` · `failed`

### `GET /questions/jobs/{job_id}/stream`
Server-Sent Events stream for real-time job progress. Used by the frontend.

### `GET /questions/books`
List all ingested books with stats.
```json
{
  "books": [{
    "book_id": "IntroductoryBusinessStatistics-OP",
    "display_name": "Introductory Business Statistics",
    "total_chunks": 585,
    "total_chapters": 13,
    "with_tables": 191,
    "with_math": 345,
    "with_images": 384,
    "chapters": [{ "num": 1, "title": "Sampling and Data" }, ...],
    "ingested_at": "2026-06-04T20:53:53Z"
  }]
}
```

### `GET /questions/books/cache`
List incomplete (resumable) ingestion checkpoints.

### `GET /questions/books/{book_id}`
Stats for a single book.

### `DELETE /questions/books/{book_hash}/cache`
Delete ingestion checkpoint + partial chunks. Next upload of the same PDF starts from page 1.

---

## Question Generation

### `POST /questions/generate/from-book`
Start question generation from an already-ingested book.
```json
{
  "book_id": "IntroductoryBusinessStatistics-OP",
  "question_type": "mcq",
  "count_per_chapter": 5,
  "difficulty": "all",
  "chapter_nums": [1, 2, 3]
}
```
- `question_type`: `mcq` · `short_answer` · `true_false`
- `difficulty`: `easy` · `medium` · `hard` · `all`
- `chapter_nums`: omit to generate for all chapters

Returns a `job_id` to poll.

### `GET /questions`
List questions with optional filters.

Query params: `topic_tag` · `difficulty` · `question_type` · `bloom_level` · `skip` · `limit`

### `GET /questions/{question_id}`
Single question detail.

### `PUT /questions/{question_id}`
Update a question (edit text, rubric, model answer, max_marks).

### `DELETE /questions/{question_id}`
Delete a question.

---

## Submissions and Marking

### `POST /submissions`
Submit a student answer.
```json
{
  "question_id": "...",
  "student_id": "...",
  "answer_text": "The mean is the sum divided by the count."
}
```

### `GET /submissions/{submission_id}`
Get a submission with marking result.
```json
{
  "submission_id": "...",
  "answer_text": "...",
  "auto_mark": 2.5,
  "max_marks": 3,
  "auto_feedback": "Good explanation of the mean. Missing the formal notation.",
  "is_marked": true,
  "is_flagged": false
}
```

### `POST /marking/mark/{submission_id}`
Trigger marking for a specific submission.

### `GET /submissions`
List submissions. Params: `student_id` · `question_id` · `is_marked` · `is_flagged`

---

## Analytics

### `GET /analytics/overview`
Summary stats: total questions, submissions, marked count, average score.

### `GET /analytics/by-topic`
Per-topic breakdown of question count and average student score.

### `GET /analytics/by-student`
Per-student performance summary.

---

## Admin

All admin endpoints require instructor role.

### `GET /admin/api-status`
Live probe of all API providers + rotation stats.
```json
{
  "live_probes": [
    { "provider": "gemini_embed", "reachable": true, "status_code": 200 },
    { "provider": "openai_embed", "reachable": true, "status_code": 200 },
    { "provider": "openai_generation", "reachable": true, "model": "gpt-4o-mini" },
    { "provider": "anthropic", "reachable": true, "model": "claude-haiku-4-5-20251001" }
  ],
  "rotation_stats": [...],
  "config": {
    "embedding_chain": ["gemini_embed (free, 768-dim)", "openai_embed (paid, 768-dim)"],
    "vision_chain": ["openai_vision (gpt-4o-mini)", "anthropic_vision (claude-haiku)"],
    ...
  }
}
```

### `POST /admin/api-status/reset-cooldowns`
Reset all provider cooldowns (use after adding new API keys or resolving quota issues).

### `POST /admin/clean/all`
Trigger text cleaning on all stored chunks. Runs on `worker-clean`.

### `POST /admin/clean/book/{book_id}`
Trigger text cleaning for a specific book.

### `GET /admin/clean/preview/{book_id}`
Preview noisy chunks before cleaning. Returns the top 10 noisiest chunks with before/after comparison.

---

## Export

### `GET /export/questions`
Export questions as CSV or JSON.

Params: `format` (`csv` · `json`) · `topic_tag` · `difficulty`

### `GET /export/submissions`
Export submissions and marking results.

---

## Error Responses

| Code | Meaning |
|---|---|
| `400` | Bad request (validation error) |
| `401` | Missing or invalid token |
| `403` | Insufficient role (student trying instructor endpoint) |
| `404` | Resource not found |
| `413` | PDF too large (over 25 MB) |
| `422` | Unprocessable entity (FastAPI validation) |
| `429` | Rate limited |
| `500` | Server error (check worker logs) |
