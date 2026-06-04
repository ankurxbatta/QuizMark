"""
api_key_manager.py — Smart API provider rotation with quota/rate-limit detection.

Tracks health of each provider per capability (embeddings, vision, generation).
When a provider returns 429 or a quota-exhaustion error it is put in cooldown
and the next available provider is tried automatically.

Usage stats are stored in MongoDB `api_usage_stats` for the dashboard endpoint.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable, TypeVar, Any

log = logging.getLogger(__name__)

T = TypeVar("T")

# Cooldown in seconds after a rate-limit hit before retrying the provider.
_COOLDOWN_RATE_LIMIT = 60        # 1 min for 429
_COOLDOWN_QUOTA_EXHAUSTED = 3600 # 1 hr for daily/monthly quota exhaustion


@dataclass
class _ProviderHealth:
    name: str
    requests: int = 0
    errors: int = 0
    last_error: str = ""
    last_error_code: int = 0
    rate_limited_until: float = 0.0   # epoch seconds
    quota_exhausted_until: float = 0.0

    @property
    def available(self) -> bool:
        now = time.time()
        return now > self.rate_limited_until and now > self.quota_exhausted_until

    @property
    def status(self) -> str:
        now = time.time()
        if now < self.quota_exhausted_until:
            remaining = int(self.quota_exhausted_until - now)
            return f"quota_exhausted (cooldown {remaining}s)"
        if now < self.rate_limited_until:
            remaining = int(self.rate_limited_until - now)
            return f"rate_limited (retry in {remaining}s)"
        return "ok"

    def record_success(self) -> None:
        self.requests += 1

    def record_rate_limit(self, retry_after: float = 0) -> None:
        self.errors += 1
        self.last_error_code = 429
        self.last_error = "Rate limited"
        delay = max(retry_after, _COOLDOWN_RATE_LIMIT)
        self.rate_limited_until = time.time() + delay
        log.warning(f"[api_key_manager] {self.name} rate-limited — cooldown {delay:.0f}s")

    def record_quota_exhausted(self, msg: str = "") -> None:
        self.errors += 1
        self.last_error_code = 429
        self.last_error = msg or "Quota exhausted"
        self.quota_exhausted_until = time.time() + _COOLDOWN_QUOTA_EXHAUSTED
        log.warning(f"[api_key_manager] {self.name} quota exhausted — cooldown {_COOLDOWN_QUOTA_EXHAUSTED}s")

    def record_error(self, code: int, msg: str) -> None:
        self.errors += 1
        self.last_error_code = code
        self.last_error = msg[:200]

    def to_dict(self) -> dict:
        return {
            "provider": self.name,
            "status": self.status,
            "requests": self.requests,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_error_code": self.last_error_code,
        }


def _is_quota_exhaustion(status_code: int, body: str) -> bool:
    """Distinguish daily/monthly quota exhaustion from transient rate limits."""
    body_lower = body.lower()
    quota_phrases = (
        "quota exceeded", "quota_exceeded", "exhausted", "billing",
        "resource_exhausted", "insufficient_quota", "rate_limit_exceeded",
        "per day", "daily limit", "monthly limit", "free tier",
    )
    return status_code in {429, 402, 403} and any(p in body_lower for p in quota_phrases)


class ApiKeyManager:
    """
    Central rotation manager.  Call `with_fallback(capability, providers, fn)`
    to execute `fn(provider_name)` trying each provider in order until one
    succeeds or all fail.

    Capabilities: "embedding", "vision", "generation", "math"
    """

    def __init__(self) -> None:
        self._health: dict[str, _ProviderHealth] = {}
        self._lock = asyncio.Lock()

    def _get(self, name: str) -> _ProviderHealth:
        if name not in self._health:
            self._health[name] = _ProviderHealth(name=name)
        return self._health[name]

    def mark_rate_limited(self, provider: str, retry_after: float = 0) -> None:
        self._get(provider).record_rate_limit(retry_after)

    def mark_quota_exhausted(self, provider: str, msg: str = "") -> None:
        self._get(provider).record_quota_exhausted(msg)

    def mark_success(self, provider: str) -> None:
        self._get(provider).record_success()

    def mark_error(self, provider: str, code: int, msg: str) -> None:
        self._get(provider).record_error(code, msg)

    def is_available(self, provider: str) -> bool:
        return self._get(provider).available

    async def with_fallback(
        self,
        capability: str,
        providers: list[str],
        fn: Callable[[str], Awaitable[T]],
    ) -> T:
        """
        Try each provider in `providers` until one succeeds.
        Raises the last exception if all fail.
        """
        last_exc: Exception = RuntimeError(f"No providers available for {capability}")
        for name in providers:
            health = self._get(name)
            if not health.available:
                log.debug(f"[api_key_manager] Skipping {name} ({health.status})")
                continue
            try:
                result = await fn(name)
                health.record_success()
                if name != providers[0]:
                    log.info(f"[api_key_manager] {capability} served by fallback provider: {name}")
                return result
            except Exception as exc:
                msg = str(exc)
                code = getattr(getattr(exc, "response", None), "status_code", 0)
                if "429" in msg or code == 429:
                    if _is_quota_exhaustion(429, msg):
                        health.record_quota_exhausted(msg)
                    else:
                        # Try to extract retry-after
                        import re
                        m = re.search(r"retry.after[\"']?\s*[:=]\s*([\d.]+)", msg, re.IGNORECASE)
                        delay = float(m.group(1)) if m else 0
                        health.record_rate_limit(delay)
                elif code in {402, 403} and _is_quota_exhaustion(code, msg):
                    health.record_quota_exhausted(msg)
                else:
                    health.record_error(code or 0, msg)
                    log.warning(f"[api_key_manager] {name} failed for {capability}: {msg[:120]}")
                last_exc = exc
        raise last_exc

    def stats(self) -> list[dict]:
        return [h.to_dict() for h in self._health.values()]


# Module-level singleton shared by all services.
key_manager = ApiKeyManager()
