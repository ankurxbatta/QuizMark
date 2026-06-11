from app.core.security import decode_token


def _register(client, username="alice", password="longenough8"):
    return client.post("/api/v1/auth/register", json={"username": username, "password": password})


def test_register_rejects_short_password(client):
    resp = _register(client, password="short")
    assert resp.status_code == 400


def test_register_then_duplicate(client):
    assert _register(client).status_code == 201
    assert _register(client).status_code == 409


def test_register_login_roundtrip(client):
    _register(client, username="bob", password="longenough8")
    resp = client.post("/api/v1/auth/login", json={"username": "bob", "password": "longenough8"})
    assert resp.status_code == 200
    claims = decode_token(resp.json()["access_token"])
    assert claims["role"] == "student"


def test_login_wrong_password(client):
    _register(client, username="carol", password="longenough8")
    resp = client.post("/api/v1/auth/login", json={"username": "carol", "password": "wrongpassword"})
    assert resp.status_code == 401


def test_register_rate_limited(client):
    for i in range(5):
        resp = _register(client, username=f"user{i}", password="longenough8")
        assert resp.status_code == 201
    resp = _register(client, username="user-too-many", password="longenough8")
    assert resp.status_code == 429
