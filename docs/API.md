# API Reference

Base URL: `http://localhost:8000/api/v1`

All protected endpoints require a Bearer token. Get one from `POST /auth/login`.

Interactive docs with live testing: **http://localhost:8000/docs** (disabled when `ENVIRONMENT=production`)

List endpoints support `skip` (default 0) and `limit` (default 100, max 500) query parameters.

---

## Auth

### `POST /auth/login`
```json
{ "username": "admin", "password": "your-password" }
```
Returns `{ "access_token": "...", "token_type": "bearer" }`.

Rate limited to 10 requests/minute per IP. Ten failed attempts lock the account for 5 minutes.

### `POST /auth/refresh`
Exchange a still-valid Bearer token for a fresh one (sliding session). No body.
Returns the same shape as `/auth/login`.

The original login time travels in the token's `auth_time` claim; once the session
is older than `SESSION_MAX_MINUTES` (default 12 h) refresh returns `401` and the
user must log in again. The frontend calls this automatically when a token has
under 10 minutes of life left, so long ingestion/generation jobs no longer log
instructors out mid-job.

### `POST /auth/register`
```json
{ "username": "student1", "password": "min-8-characters" }
```
Creates a student account. Password must be at least 8 characters. Rate limited to 5 requests/minute per IP.

### `GET /auth/students`
List all student accounts. Instructor only.

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

### `GET /questions/jobs/{job_id}/stream?token={jwt}`
Server-Sent Events stream for real-time job progress. Used by the frontend.

Browsers' EventSource API cannot send Authorization headers, so the JWT must be passed
as the `token` query parameter. Requests with a missing or invalid token are rejected.

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
    "ingested_at": "2026-06-04T20:53:53Z",
    "index_builds": [
      { "index": "math", "status": "processing", "progress": "Enriching formulas 240/607" },
      { "index": "figure", "status": "done" },
      { "index": "table", "status": "done" }
    ]
  }]
}
```
`index_builds` reports the specialist RAG index builds that run in the
background after ingestion; the Library UI shows them until they finish.

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

## Quizzes

A quiz is a titled bundle of questions and the **unit of assignment**: an
instructor groups questions into a quiz and assigns the quiz to students. A
student's assessment is the union of all questions across the quizzes assigned
to them. Legacy per-question assignment still works — a question is answerable
if it is in an assigned quiz **or** assigned directly — so quizzes are fully
backward-compatible with question banks built before they existed.

### `GET /quizzes/mine`
Student endpoint. Returns the quizzes assigned to the calling student, each with
its questions populated in the quiz's stored order (answer keys omitted).
```json
[
  {
    "id": "...",
    "title": "Probability — Week 3",
    "description": "Conditional probability practice",
    "questions": [ { "id": "...", "question_text": "...", "question_type": "mcq", "max_marks": 1, "assets": [] } ]
  }
]
```

### `POST /quizzes`
Create a quiz. Instructor only. `question_ids` are de-duplicated, order-preserved,
and validated (unknown ids → `400`).
```json
{ "title": "Probability — Week 3", "description": "optional", "question_ids": ["...", "..."] }
```
Returns the created quiz (`201`):
```json
{
  "id": "...",
  "title": "Probability — Week 3",
  "description": "optional",
  "question_ids": ["...", "..."],
  "question_count": 2,
  "assigned_student_ids": [],
  "created_at": "2026-06-22T10:00:00Z"
}
```

### `GET /quizzes`
List all quizzes (newest first). Instructor only.

### `GET /quizzes/{quiz_id}`
Single quiz detail. Instructor only.

### `PUT /quizzes/{quiz_id}`
Update `title`, `description`, and/or `question_ids` (any subset). Instructor
only. Re-supplying `question_ids` replaces the set (re-validated and re-ordered).

### `DELETE /quizzes/{quiz_id}`
Delete a quiz (`204`). Instructor only.

### `GET /quizzes/{quiz_id}/assignees`
List the student ids the quiz is assigned to. Instructor only.
```json
{ "quiz_id": "...", "student_ids": ["...", "..."] }
```

### `PUT /quizzes/{quiz_id}/assignees`
Replace the quiz's assignee list. Instructor only. Each id must be an existing
student account (unknown ids → `400`).
```json
{ "student_ids": ["...", "..."] }
```

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
Returns `409 Conflict` if the student has already submitted an answer for
this question — one submission per student per question. The assessment UI
shows existing results on reload instead of an empty form.

MCQ and True/False submissions are marked instantly by comparing against the
question's stored `correct_answer` key — no model call. Short answers go
through the SLM + RAG + LLM marking pipeline.

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

### `GET /submissions`
List submissions (instructor). Params: `student_id` · `question_id` · `is_marked` · `is_flagged` · `skip` · `limit`

### `GET /submissions/my`
List the calling student's own submissions. Params: `skip` · `limit`

---

## Marking (instructor)

### `POST /marking/{submission_id}/retry`
Re-queue AI marking for a submission. Marking is idempotent — a submission already
being marked by another worker is skipped, and stale claims (>10 min) are retaken.

### `PUT /marking/{submission_id}/override`
Override the AI mark with an instructor mark + reason. Writes an audit-log entry
recording the actor, the previous mark, and the new mark.

### `GET /marking/flagged`
Submissions flagged for instructor review (low marking confidence).

### `GET /marking/audit-log`
Mark-override audit trail.

---

## Analytics (instructor)

### `GET /analytics/pipeline`
Marking pipeline stats: counts per confidence band, flagged rate, auto vs overridden marks.

### `GET /analytics/questions`
Per-question performance breakdown. Params: `skip` · `limit`

### `GET /analytics/confidence-distribution`
Distribution of marking confidence across submissions. Params: `skip` · `limit`

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

### `POST /admin/index/build/{book_id}`
Build (or rebuild) the specialist RAG indexes for one book. Runs on `worker-math`.

### `POST /admin/index/build-all`
Build (or rebuild) specialist indexes for every ingested book.

### `GET /admin/index/status`
Per-book build status and document counts for the specialist indexes.

### `POST /admin/questions/latexify`
Backfill: wrap bare math in already-stored questions as `$`-delimited LaTeX so
the frontend can render it with KaTeX. Formats questions generated before the
math-rendering pass existed. Optionally scope to one book with `?book_id=…`;
omit to backfill every stored question. Non-fatal — questions that fail
formatting are left untouched.

---

## Export (instructor)

CSV responses are streamed (constant memory regardless of data size) and
formula-injection safe: cell values starting with `=`, `+`, `-`, `@` are
prefixed with an apostrophe so spreadsheets treat them as text.

### `GET /export/marks`
Export all submissions with marks, feedback, and question context as CSV.

### `GET /export/audit`
Export the mark-override audit log as CSV.

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
