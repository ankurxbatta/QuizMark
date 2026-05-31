"""
llm_service.py  —  Unified LLM adapter.

All AI work is routed through Gemini. AnthropicClient and OpenAIClient
are available as optional overrides via ONLINE_LLM_PROVIDER config.

Public singletons:
  slm_service        → GeminiClient (embeddings + fast scoring)
  llm_service        → GeminiClient (generation fallback)
  online_service     → GeminiClient / AnthropicClient / OpenAIClient (marking)
  generation_service → GeminiClient / AnthropicClient / OpenAIClient (question generation)

All share the same interface:  .generate(prompt) → str
                                .embed(text)      → list[float]
                                .describe_image() → str  (GeminiClient only)
"""
import asyncio
import base64
import logging
import re

import httpx
from app.core.config import settings

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _redact_url_secrets(message: str) -> str:
    return re.sub(r"([?&]key=)[^&\s']+", r"\1***", message)


async def _sleep_before_retry(resp: httpx.Response, attempt: int) -> None:
    retry_after = resp.headers.get("retry-after")
    try:
        delay = float(retry_after) if retry_after else 0.0
    except ValueError:
        delay = 0.0
    await asyncio.sleep(max(delay, 5.0 * (attempt + 1)))


class GeminiClient:
    """Calls the Google Gemini Generative Language API (generation + embeddings + vision)."""

    def __init__(self, model: str | None = None, max_tokens: int | None = None):
        self.model = (model or settings.GENERATION_LLM_MODEL).removeprefix("models/")
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        self._base = settings.GEMINI_BASE_URL

    async def generate(self, prompt: str) -> str:
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        endpoint = f"{self._base}/models/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": settings.LLM_TEMPERATURE,
                "maxOutputTokens": self.max_tokens,
            },
        }
        if self.model.startswith("gemini-2.5-flash"):
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    endpoint, params={"key": settings.GEMINI_API_KEY}, json=payload
                )
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 4:
                await _sleep_before_retry(resp, attempt)
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(_redact_url_secrets(str(exc))) from exc
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    async def embed(self, text: str) -> list[float]:
        """768-dim embeddings via gemini-embedding-001 with outputDimensionality=768."""
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        endpoint = f"{self._base}/models/{settings.GEMINI_EMBEDDING_MODEL}:embedContent"
        payload = {
            "model": f"models/{settings.GEMINI_EMBEDDING_MODEL}",
            "content": {"parts": [{"text": text[:2048]}]},
            "taskType": "SEMANTIC_SIMILARITY",
            "outputDimensionality": 768,
        }
        for attempt in range(6):
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    endpoint, params={"key": settings.GEMINI_API_KEY}, json=payload
                )
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 5:
                await _sleep_before_retry(resp, attempt)
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(_redact_url_secrets(str(exc))) from exc
            return resp.json()["embedding"]["values"]

    async def describe_image(self, image_bytes: bytes, context: str = "") -> str:
        """Describe a chart/graph image using Gemini Vision."""
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        prompt = (
            "You are an expert at reading statistical charts and graphs in textbooks. "
            "Describe what the chart or graph shows. Focus on: the type of visualisation, "
            "axis labels, key values, trends, and what the data demonstrates. "
            "If no meaningful chart is present, respond with exactly: NO_CHART"
        )
        if context:
            prompt = f"Context: {context}\n\n{prompt}"
        payload = {
            "contents": [{"parts": [
                {"text": prompt},
                {"inlineData": {"mimeType": "image/png", "data": b64}},
            ]}],
            "generationConfig": {
                "maxOutputTokens": 400,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self._base}/models/{self.model}:generateContent",
                    params={"key": settings.GEMINI_API_KEY},
                    json=payload,
                )
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 4:
                await _sleep_before_retry(resp, attempt)
                continue
            resp.raise_for_status()
            result = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            return "" if result == "NO_CHART" else result
        return ""


class AnthropicClient:
    """Calls the Anthropic Messages API (Claude). Used when ANTHROPIC_API_KEY is set."""

    def __init__(self, model: str | None = None, max_tokens: int | None = None):
        self.model = model or settings.ONLINE_LLM_MODEL
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS

    async def generate(self, prompt: str) -> str:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]

    async def embed(self, text: str) -> list[float]:
        return await slm_service.embed(text)


class OpenAIClient:
    """Calls the OpenAI Chat Completions API. Retained for optional use."""

    def __init__(self, model: str | None = None):
        self.model = model or settings.ONLINE_LLM_MODEL

    async def generate(self, prompt: str) -> str:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={
                    "model": self.model,
                    "temperature": 0.2,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={"model": "text-embedding-3-small", "input": text},
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]


# ── Module-level singletons ─────────────────────────────────────────────────────
# All AI work goes through Gemini.

# Tier-1: "SLM" — now Gemini flash (better accuracy, no local compute)
slm_service = GeminiClient(
    model=settings.GENERATION_LLM_MODEL,
    max_tokens=256,
)

# Tier-3 offline fallback — same Gemini client
llm_service = GeminiClient(
    model=settings.GENERATION_LLM_MODEL,
    max_tokens=settings.LLM_MAX_TOKENS,
)


def _build_online_client():
    if not settings.ONLINE_LLM_ENABLED:
        return None
    if settings.ONLINE_LLM_PROVIDER == "anthropic" and settings.ANTHROPIC_API_KEY:
        return AnthropicClient(model=settings.ONLINE_LLM_MODEL)
    if settings.ONLINE_LLM_PROVIDER == "openai" and settings.OPENAI_API_KEY:
        return OpenAIClient(model=settings.ONLINE_LLM_MODEL)
    if settings.ONLINE_LLM_PROVIDER == "gemini" and settings.GEMINI_API_KEY:
        return GeminiClient(model=settings.ONLINE_LLM_MODEL)
    return None


online_service = _build_online_client()


def _build_generation_client():
    if settings.GENERATION_LLM_PROVIDER == "anthropic" and settings.ANTHROPIC_API_KEY:
        return AnthropicClient(
            model=settings.GENERATION_LLM_MODEL,
            max_tokens=settings.GENERATION_MAX_TOKENS,
        )
    if settings.GENERATION_LLM_PROVIDER == "openai" and settings.OPENAI_API_KEY:
        return OpenAIClient(model=settings.GENERATION_LLM_MODEL)
    if settings.GENERATION_LLM_PROVIDER == "gemini" and settings.GEMINI_API_KEY:
        return GeminiClient(
            model=settings.GENERATION_LLM_MODEL,
            max_tokens=settings.GENERATION_MAX_TOKENS,
        )
    # Final fallback: use llm_service (Gemini)
    return llm_service


generation_service = _build_generation_client()
