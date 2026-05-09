# API Reference

Base URL: `http://localhost:8000/api/v1`
Interactive docs: `http://localhost:8000/docs`

All authenticated endpoints require:
```
Authorization: Bearer <jwt_token>
```

---

## Authentication

### POST /auth/login
**Request body**
```json
{ "username": "instructor1", "password": "secret" }
```
**Response 200**
```json
{ "access_token": "eyJ...", "token_type": "bearer" }
```
**Errors**: `401` invalid credentials · `403` account locked

---

## Questions

### GET /questions/
List all questions. Supports `?topic=` and `?difficulty=` filters.

### GET /questions/count
Returns `{ "total": 200 }`.

### GET /questions/{id}
Retrieve a single question by UUID.

### POST /questions/
**Request body**
```json
{
  "question_text": "What is a p-value?",
  "question_type": "short_answer",
  "model_answer": "Probability of data this extreme given H0...",
  "rubric": "2 marks: definition. 1 mark: decision rule...",
  "max_marks": 5,
  "topic_tag": "Hypothesis Testing",
  "difficulty": "medium"
}
```

### PUT /questions/{id}
Same body as POST. Returns updated question.

### DELETE /questions/{id}
Returns `204 No Content`.

### POST /questions/generate
Generate questions from uploaded text content via LLM.

**Form data**
- `file` – `.txt` file with source material
- `question_type` – `short_answer | mcq | true_false`
- `count` – integer 1–50

**Response**
```json
{ "generated": 20 }
```

---

## Submissions

### POST /submissions/
Submit a student answer. Triggers async marking.
```json
{ "question_id": "uuid", "answer_text": "The p-value is..." }
```

### GET /submissions/
List all submissions. Use `?flagged_only=true` to filter.

### GET /submissions/{id}
Retrieve submission with marking results.

---

## Marking

### PUT /marking/{id}/override
```json
{
  "override_mark": 4.0,
  "override_feedback": "Good, but missed the CLT condition.",
  "override_reason": "Auto-mark undervalued partial credit."
}
```

### GET /marking/flagged
List all submissions flagged for human review.

### GET /marking/audit-log
Return all audit log entries as JSON array.

---

## Export

### GET /export/marks
Download all submission marks as CSV.
Columns: `student_id, question_id, mark, max_mark, feedback, override_flag, timestamp`

### GET /export/audit
Download full audit log as CSV.

---

## Health

### GET /health
Returns `{ "status": "ok" }`.

---

## Error Codes

| Code | Meaning |
|------|---------|
| 400 | Bad request / validation error |
| 401 | Missing or invalid JWT |
| 403 | Account locked or insufficient role |
| 404 | Resource not found |
| 422 | Pydantic validation failure |
| 500 | Internal server error |
