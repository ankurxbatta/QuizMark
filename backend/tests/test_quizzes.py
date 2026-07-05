import pytest


async def _seed_question(db, qid="q1", text="What is 2+2?"):
    await db["questions"].insert_one({
        "_id": qid, "question_text": text, "question_type": "short_answer",
        "model_answer": "4", "rubric": "", "max_marks": 2,
        "assigned_student_ids": [], "created_at": __import__("datetime").datetime.utcnow(),
    })


async def _seed_student(db, sid="stud-1", username="stud1"):
    await db["users"].insert_one({"_id": sid, "username": username, "role": "student"})


def _ibearer(token_factory):
    return {"Authorization": f"Bearer {token_factory('instructor')}"}


# ── RBAC ────────────────────────────────────────────────────────────────────────

def test_create_quiz_requires_auth(client):
    assert client.post("/api/v1/quizzes/", json={"title": "Q"}).status_code == 401


def test_create_quiz_forbidden_for_students(client, token_factory):
    r = client.post("/api/v1/quizzes/", json={"title": "Q"},
                    headers={"Authorization": f"Bearer {token_factory('student')}"})
    assert r.status_code == 403


# ── CRUD + assignment flow ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quiz_lifecycle(client, token_factory, mock_db):
    await _seed_question(mock_db, "q1")
    await _seed_question(mock_db, "q2", "What is 3+3?")
    await _seed_student(mock_db, "stud-1", "stud1")
    h = _ibearer(token_factory)

    # create with questions
    r = client.post("/api/v1/quizzes/", json={"title": "Algebra", "question_ids": ["q1", "q2"]}, headers=h)
    assert r.status_code == 201, r.text
    quiz = r.json()
    assert quiz["question_count"] == 2
    qid = quiz["id"]

    # list + get
    assert any(q["id"] == qid for q in client.get("/api/v1/quizzes/", headers=h).json())
    assert client.get(f"/api/v1/quizzes/{qid}", headers=h).json()["title"] == "Algebra"

    # assign to the student
    r = client.put(f"/api/v1/quizzes/{qid}/assignees", json={"student_ids": ["stud-1"]}, headers=h)
    assert r.status_code == 200
    assert r.json()["student_ids"] == ["stud-1"]

    # student sees the quiz with populated, order-preserved questions
    sh = {"Authorization": f"Bearer {token_factory('student', 'stud-1')}"}
    mine = client.get("/api/v1/quizzes/mine", headers=sh).json()
    assert len(mine) == 1
    assert [q["id"] for q in mine[0]["questions"]] == ["q1", "q2"]

    # assessment endpoint also surfaces quiz questions
    assessment = client.get("/api/v1/questions/assessment", headers=sh).json()
    assert {q["id"] for q in assessment} == {"q1", "q2"}

    # delete
    assert client.delete(f"/api/v1/quizzes/{qid}", headers=h).status_code == 204
    assert client.get(f"/api/v1/quizzes/{qid}", headers=h).status_code == 404


@pytest.mark.asyncio
async def test_create_quiz_rejects_unknown_question(client, token_factory, mock_db):
    r = client.post("/api/v1/quizzes/", json={"title": "Bad", "question_ids": ["nope"]}, headers=_ibearer(token_factory))
    assert r.status_code == 400
    assert "nope" in r.json()["detail"]


@pytest.mark.asyncio
async def test_assign_rejects_non_student(client, token_factory, mock_db):
    await _seed_question(mock_db, "q1")
    h = _ibearer(token_factory)
    qid = client.post("/api/v1/quizzes/", json={"title": "Q", "question_ids": ["q1"]}, headers=h).json()["id"]
    r = client.put(f"/api/v1/quizzes/{qid}/assignees", json={"student_ids": ["ghost"]}, headers=h)
    assert r.status_code == 400
