import time
from datetime import timedelta

import pytest
from fastapi import HTTPException
from jose import JWTError

from app.core.security import (
    SlidingWindowLimiter,
    create_access_token,
    decode_token,
    hash_password,
    require_instructor,
    verify_password,
)


def test_password_hash_roundtrip():
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)


def test_token_roundtrip():
    token = create_access_token({"sub": "u1", "role": "instructor"})
    claims = decode_token(token)
    assert claims["sub"] == "u1"
    assert claims["role"] == "instructor"
    assert "exp" in claims


def test_expired_token_rejected():
    token = create_access_token({"sub": "u1"}, expires_delta=timedelta(minutes=-1))
    with pytest.raises(JWTError):
        decode_token(token)


def test_require_instructor_rejects_student():
    with pytest.raises(HTTPException) as exc:
        require_instructor({"sub": "u1", "role": "student"})
    assert exc.value.status_code == 403


def test_require_instructor_allows_instructor():
    claims = {"sub": "u1", "role": "instructor"}
    assert require_instructor(claims) == claims


def test_rate_limiter_blocks_after_max_calls():
    limiter = SlidingWindowLimiter(max_calls=3, window_seconds=60.0)
    for _ in range(3):
        limiter.check("1.2.3.4")
    with pytest.raises(HTTPException) as exc:
        limiter.check("1.2.3.4")
    assert exc.value.status_code == 429


def test_rate_limiter_keys_are_independent():
    limiter = SlidingWindowLimiter(max_calls=1, window_seconds=60.0)
    limiter.check("a")
    limiter.check("b")  # must not raise


def test_rate_limiter_window_expires():
    limiter = SlidingWindowLimiter(max_calls=1, window_seconds=0.05)
    limiter.check("a")
    time.sleep(0.06)
    limiter.check("a")  # must not raise after the window passed
