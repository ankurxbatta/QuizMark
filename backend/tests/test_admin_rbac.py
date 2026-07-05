class _FakeAsyncResult:
    id = "fake-task-id"


def test_clean_all_requires_auth(client):
    resp = client.post("/api/v1/admin/clean/all")
    assert resp.status_code == 401


def test_clean_all_forbidden_for_students(client, token_factory):
    resp = client.post(
        "/api/v1/admin/clean/all",
        headers={"Authorization": f"Bearer {token_factory('student')}"},
    )
    assert resp.status_code == 403


def test_clean_all_allowed_for_instructors(client, token_factory, monkeypatch):
    from app.tasks import clean_tasks

    monkeypatch.setattr(clean_tasks.clean_all_chunks_task, "delay", lambda: _FakeAsyncResult())
    resp = client.post(
        "/api/v1/admin/clean/all",
        headers={"Authorization": f"Bearer {token_factory('instructor')}"},
    )
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "fake-task-id"


def test_reset_cooldowns_forbidden_for_students(client, token_factory):
    resp = client.post(
        "/api/v1/admin/api-status/reset-cooldowns",
        headers={"Authorization": f"Bearer {token_factory('student')}"},
    )
    assert resp.status_code == 403
