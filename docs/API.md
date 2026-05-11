# API endpoints

Base URL: `http://localhost:8000/api/v1`

You can also use the interactive Swagger docs at `http://localhost:8000/docs` which lets you test endpoints directly in the browser — probably easier than reading this.

All endpoints except `/auth/login` and `/health` need a JWT token in the header:
```
Authorization: Bearer <your_token>
```

---

## Login

### POST /auth/login

```json
{ "username": "admin", "password": "yourpassword" }
```

Returns:
```json
{ "access_token": "eyJ...", "token_type": "bearer" }
```

Use the `access_token` value in the Authorization header for everything else.

Errors: `401` wrong credentials, `403` account locked after 3 failed attempts (locked for 5 minutes)

---

## Generating questions

### POST /questions/chapters

Scans a PDF and returns the chapters it found. Useful before generating so you can filter to one chapter. Doesn't create anything in the database.

Send as `multipart/form-data` with a `file` field (PDF only).

Example response:
```json
{
  "chapters": [
    { "num": 1, "title": "Sampling and Data" },
    { "num": 2, "title": "Descriptive Statistics" }
  ]
}
```

If no chapters are found it returns `[{ "num": 0, "title": "Entire Document" }]`.

---

### POST /questions/generate

Main generation endpoint. Takes a file and some options, runs the pipeline, and saves the generated questions to the database.

Send as `multipart/form-data`:

| Field | Required | Description |
|---|---|---|
| `file` | yes | `.pdf` or `.txt`, max 25 MB |
| `question_type` | yes | `short_answer`, `mcq`, or `true_false` |
| `count` | yes | How many questions to generate, 1–50 |
| `topic_filter` | no | Filter to a specific chapter (use a title from `/questions/chapters`) |

Example response:
```json
{
  "generated": 15,
  "source_file": "stats_chapter3.pdf",
  "source_pages": 48,
  "chunks_processed": 6,
  "topics_covered": ["Normal Distribution"],
  "questions": [
    {
      "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "question_text": "What does a z-score tell you about a data point?",
      "question_type": "short_answer",
      "model_answer": "A z-score tells you how many standard deviations a data point is from the mean. A positive z-score means the value is above the mean, negative means below.",
      "rubric": "1 mark: mentions standard deviations from mean. 1 mark: explains direction (above/below).",
      "max_marks": 2.0,
      "topic_tag": "Normal Distribution",
      "difficulty": "easy",
      "source_page_range": "45-52",
      "source_chunk": "Ch3 § Standard Normal",
      "created_at": "2026-05-10T12:00:00Z"
    }
  ]
}
```

Error responses:
- `413` — file too big
- `415` — wrong file type
- `422` — PDF has no readable text (probably a scanned image)
- `500` — LLM didn't return anything usable

---

### POST /questions/generate/async

Same as above but runs as a background job instead of waiting. Use this for full textbooks that would time out otherwise. Only accepts PDFs.

Send as `multipart/form-data`:

| Field | Required | Description |
|---|---|---|
| `file` | yes | `.pdf` only |
| `question_type` | yes | `short_answer`, `mcq`, or `true_false` |
| `count_per_chapter` | yes | Questions per chapter, 1–50 |

Returns immediately with a job ID:
```json
{
  "job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "filename": "bigstatisticsbook.pdf",
  "total_pages": 631,
  "status": "queued"
}
```

Then poll to check progress.

---

### GET /questions/jobs/{job_id}

Check how an async job is going.

```json
{
  "job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "filename": "bigstatisticsbook.pdf",
  "total_pages": 631,
  "status": "processing",
  "chapters_done": 4,
  "questions_created": 83,
  "error": null,
  "started_at": "2026-05-10T12:00:00Z",
  "completed_at": null
}
```

Status goes: `queued` → `processing` → `completed` or `failed`. If it fails, `error` will have a message.

---

## Managing questions

### GET /questions/

Returns all questions. Filter with query params:
- `?topic=Normal Distribution`
- `?difficulty=easy` (or `medium` / `hard`)

### GET /questions/count

Just returns `{ "total": 42 }`.

### GET /questions/topics

Returns every topic with a count:
```json
[
  { "topic": "Normal Distribution", "count": 12 },
  { "topic": "Hypothesis Testing", "count": 9 }
]
```

### GET /questions/{id}

Get one question by its UUID.

### POST /questions/

Add a question manually if you want to write your own.

```json
{
  "question_text": "Explain the central limit theorem in your own words.",
  "question_type": "short_answer",
  "model_answer": "The central limit theorem states that the sampling distribution of the mean approaches a normal distribution as the sample size increases, regardless of the population distribution.",
  "rubric": "2 marks: correct statement about sampling distribution of the mean. 1 mark: mentions sample size increasing. 1 mark: regardless of population shape.",
  "max_marks": 4,
  "topic_tag": "Sampling Distributions",
  "difficulty": "medium"
}
```

### PUT /questions/{id}

Update a question. Same fields as POST. Also regenerates the embedding.

### DELETE /questions/{id}

Deletes the question. Returns `204`.

---

## Health check

### GET /health

Returns `{ "status": "ok" }` if the server is up.

---

## Error reference

| Code | What it means |
|---|---|
| 400 | Something wrong with the request |
| 401 | Not logged in / token expired |
| 403 | Account locked |
| 404 | That ID doesn't exist |
| 413 | File too large |
| 415 | Wrong file type |
| 422 | Validation failed (check the error body for details) |
| 500 | Something broke server-side — check `docker compose logs backend` |
