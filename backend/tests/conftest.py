"""
Test fixtures. Required settings are injected as env vars BEFORE any app import
so `app.core.config.Settings()` never depends on a local .env file.
"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")
os.environ.setdefault("ADMIN_ENABLED", "false")
os.environ.setdefault("ENVIRONMENT", "development")

import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.core.security import create_access_token, login_limiter, register_limiter


@pytest.fixture()
def mock_db():
    return AsyncMongoMockClient()["test_db"]


@pytest.fixture()
def client(mock_db):
    # TestClient is deliberately NOT used as a context manager: that would run
    # the startup hooks, which try to connect to a real MongoDB.
    from app.main import app
    from app.core.database import get_db

    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def reset_rate_limiters():
    login_limiter._hits.clear()
    register_limiter._hits.clear()
    yield
    login_limiter._hits.clear()
    register_limiter._hits.clear()


@pytest.fixture()
def token_factory():
    def make(role: str = "student", sub: str = "user-1") -> str:
        return create_access_token({"sub": sub, "role": role})

    return make
