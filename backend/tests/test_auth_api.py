import time

from app.core.security import create_access_token, decode_token


def _register(client, username="alice", password="longenough8"):
    return client.post("/api/v1/auth/register", json={"username": username, "password": password})


def _login(client, username, password="longenough8"):
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


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


def test_refresh_returns_fresh_token(client):
    _register(client, username="dave")
    token = _login(client, "dave")
    time.sleep(1.1)  # ensure the new exp differs (exp has 1-second resolution)
    resp = client.post("/api/v1/auth/refresh", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    old, new = decode_token(token), decode_token(resp.json()["access_token"])
    assert new["sub"] == old["sub"]
    assert new["role"] == old["role"]
    assert new["auth_time"] == old["auth_time"]  # original login time is preserved
    assert new["exp"] > old["exp"]


def test_refresh_requires_auth(client):
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_refresh_rejects_expired_session(client):
    _register(client, username="erin")
    token = _login(client, "erin")
    sub = decode_token(token)["sub"]
    # Forge a token whose original login is older than the session cap.
    stale = create_access_token({"sub": sub, "role": "student", "auth_time": int(time.time()) - 13 * 3600})
    resp = client.post("/api/v1/auth/refresh", headers={"Authorization": f"Bearer {stale}"})
    assert resp.status_code == 401


def test_refresh_rejects_deleted_user(client):
    _register(client, username="frank")
    token = _login(client, "frank")
    stranger = create_access_token({"sub": "no-such-user-id", "role": "student", "auth_time": int(time.time())})
    resp = client.post("/api/v1/auth/refresh", headers={"Authorization": f"Bearer {stranger}"})
    assert resp.status_code == 401
    # sanity: the real user still refreshes fine
    assert client.post("/api/v1/auth/refresh", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_register_rate_limited(client):
    from app.core.security import register_limiter
    for i in range(register_limiter.max_calls):
        resp = _register(client, username=f"user{i}", password="longenough8")
        assert resp.status_code == 201
    resp = _register(client, username="user-too-many", password="longenough8")
    assert resp.status_code == 429


def test_login_limiter_is_per_account_not_per_ip(client):
    """A classroom behind one NAT IP must not lock each other out: hammering
    one account trips its limit, but a different account still signs in."""
    _register(client, username="victim", password="longenough8")
    _register(client, username="bystander", password="longenough8")
    from app.core.security import login_limiter
    for _ in range(login_limiter.max_calls):
        client.post("/api/v1/auth/login", json={"username": "victim", "password": "wrongpassword"})
    resp = client.post("/api/v1/auth/login", json={"username": "victim", "password": "longenough8"})
    assert resp.status_code == 429
    # same IP, different account — unaffected
    resp = client.post("/api/v1/auth/login", json={"username": "bystander", "password": "longenough8"})
    assert resp.status_code == 200
