import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.JWT_EXPIRY_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """FastAPI dependency: validates the JWT and returns the claims dict."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        claims = decode_token(token)
        if not claims.get("sub"):
            raise credentials_exception
        return claims
    except JWTError:
        raise credentials_exception


def require_instructor(claims: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency: ensures the caller is an instructor."""
    if claims.get("role") != "instructor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Instructor access required",
        )
    return claims


def get_client_ip(request: Request) -> str:
    """Best-effort real client IP for rate-limiting keys.

    Behind a reverse proxy / load balancer, ``request.client.host`` is the
    proxy's address, so every client shares one rate-limit bucket. When present
    we use the LEFT-MOST entry of ``X-Forwarded-For`` (the original client).

    SECURITY ASSUMPTION: ``X-Forwarded-For`` is client-controllable and trivially
    spoofable unless a *trusted* reverse proxy overwrites/sets it. Deployments
    MUST terminate ingress behind such a proxy (nginx/ALB/Cloudflare) that
    strips inbound XFF and appends the true peer. Without that, an attacker can
    rotate the header to dodge the limiter — but that is strictly no worse than
    today's per-proxy-IP bucketing, and correct behind a proper proxy.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


class SlidingWindowLimiter:
    """Minimal in-process per-key rate limiter (suitable for single-instance deployments).

    TODO(scale): this state lives in one process's memory, so each replica keeps
    its own window — N replicas allow ~N× the intended rate, and a restart wipes
    counters. Before horizontal scaling, back this with the shared Redis already
    configured for Celery (``settings.CELERY_BROKER_URL``) using an atomic
    INCR + EXPIRE (or a sorted-set sliding window) keyed by client IP, so the
    limit is enforced cluster-wide. Left in-process for now to avoid adding a
    Redis round-trip on the hot auth path in the current single-replica deploy.
    """

    def __init__(self, max_calls: int, window_seconds: float = 60.0):
        self.max_calls = max_calls
        self.window = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Record a hit for `key`; raise 429 if it exceeds the window limit."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.max_calls:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests. Please try again shortly.",
                )
            hits.append(now)


login_limiter = SlidingWindowLimiter(max_calls=10, window_seconds=60.0)
register_limiter = SlidingWindowLimiter(max_calls=5, window_seconds=60.0)


def require_student(claims: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency: ensures the caller is a student or instructor."""
    if claims.get("role") not in ("student", "instructor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authentication required",
        )
    return claims
