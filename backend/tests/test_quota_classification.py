"""429 classification: per-minute throttles must not trigger the 1-hour cooldown."""
from app.services.api_key_manager import _is_quota_exhaustion


def test_gemini_per_minute_throttle_is_transient():
    body = (
        '429 RESOURCE_EXHAUSTED. Quota exceeded for quota metric '
        '"generativelanguage.googleapis.com/embed_content_free_tier_requests" '
        'and limit "EmbedContentRequestsPerMinutePerProjectPerModel"'
    )
    assert not _is_quota_exhaustion(429, body)


def test_openai_per_minute_rate_limit_is_transient():
    body = (
        '{"error": {"code": "rate_limit_exceeded", "message": '
        '"Rate limit reached for gpt-4o-mini: 3 requests per min."}}'
    )
    assert not _is_quota_exhaustion(429, body)


def test_openai_insufficient_quota_is_exhaustion():
    body = (
        '{"error": {"code": "insufficient_quota", "message": '
        '"You exceeded your current quota, please check your plan and billing."}}'
    )
    assert _is_quota_exhaustion(429, body)


def test_gemini_daily_quota_is_exhaustion():
    body = (
        '429 RESOURCE_EXHAUSTED. Quota exceeded for metric '
        '"embed_content_free_tier_requests" and limit '
        '"EmbedContentRequestsPerDayPerProject"'
    )
    assert _is_quota_exhaustion(429, body)


def test_non_429_is_never_exhaustion():
    assert not _is_quota_exhaustion(500, "quota exceeded per day billing")


def test_generic_resource_exhausted_alone_is_transient():
    assert not _is_quota_exhaustion(429, "RESOURCE_EXHAUSTED")
