"""
llm_service.py  —  Unified LLM adapter with automatic API key rotation.

Provider split:
  Embeddings  → Gemini gemini-embedding-001 (768-dim) → fallback OpenAI text-embedding-3-small (768-dim)
  Vision      → Gemini 2.5 Flash → fallback OpenAI GPT-4o-mini → Anthropic Claude Haiku
  Generation  → Groq llama-3.3-70b → fallback OpenAI GPT-4o-mini → Anthropic Claude Haiku
  Marking     → Mistral small → fallback Groq → fallback OpenAI

All providers share the same .generate()/.embed()/.describe_image() interface.
The key_manager singleton tracks quotas and auto-rotates on 429/quota errors.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re

import httpx
from app.core.config import settings
from app.services.api_key_manager import key_manager

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


def _redact_url_secrets(message: str) -> str:
    return re.sub(r"([?&]key=)[^&\s']+", r"\1***", message)


async def _sleep_before_retry(resp: httpx.Response, attempt: int) -> None:
    retry_after = resp.headers.get("retry-after")
    try:
        delay = float(retry_after) if retry_after else 0.0
    except ValueError:
        delay = 0.0
    await asyncio.sleep(max(delay, 5.0 * (attempt + 1)))


# ── OpenAI Embedding Client ────────────────────────────────────────────────────

class OpenAIEmbeddingClient:
    """
    OpenAI text-embedding-3-small with dimensions=768.
    Exact 768-dim output keeps MongoDB vector index compatible with Gemini embeddings.
    """

    def __init__(self) -> None:
        self._base = settings.OPENAI_BASE_URL
        self._model = settings.OPENAI_EMBEDDING_MODEL

    async def embed(self, text: str) -> list[float]:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        payload = {
            "model": self._model,
            "input": text[:8191],
            "dimensions": 768,
        }
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self._base}/embeddings",
                    headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                    json=payload,
                )
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 4:
                await _sleep_before_retry(resp, attempt)
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(str(exc)) from exc
            return resp.json()["data"][0]["embedding"]
        return []

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        # OpenAI supports array input — one round-trip for up to 2048 items
        payload = {
            "model": self._model,
            "input": [t[:8191] for t in texts],
            "dimensions": 768,
        }
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self._base}/embeddings",
                    headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                    json=payload,
                )
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 4:
                await _sleep_before_retry(resp, attempt)
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(str(exc)) from exc
            items = sorted(resp.json()["data"], key=lambda x: x["index"])
            return [item["embedding"] for item in items]
        return [[] for _ in texts]


# ── Gemini Client ──────────────────────────────────────────────────────────────

class GeminiClient:
    """Gemini — embeddings + chart vision + text generation."""

    def __init__(self, model: str | None = None, max_tokens: int | None = None):
        self.model = (model or "gemini-2.5-flash").removeprefix("models/")
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
                if resp.status_code == 429:
                    body = resp.text
                    if any(p in body.lower() for p in ("quota", "exhausted", "billing")):
                        key_manager.mark_quota_exhausted("gemini", body[:200])
                        raise RuntimeError(_redact_url_secrets(f"Gemini quota exhausted: {body[:200]}"))
                    key_manager.mark_rate_limited("gemini")
                await _sleep_before_retry(resp, attempt)
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(_redact_url_secrets(str(exc))) from exc
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    async def embed(self, text: str) -> list[float]:
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
                if resp.status_code == 429:
                    body = resp.text
                    if any(p in body.lower() for p in ("quota", "exhausted", "billing")):
                        key_manager.mark_quota_exhausted("gemini_embed", body[:200])
                        raise RuntimeError(f"Gemini embedding quota exhausted: {body[:200]}")
                    key_manager.mark_rate_limited("gemini_embed")
                await _sleep_before_retry(resp, attempt)
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(_redact_url_secrets(str(exc))) from exc
            return resp.json()["embedding"]["values"]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        model_path = f"models/{settings.GEMINI_EMBEDDING_MODEL}"
        endpoint = f"{self._base}/{model_path}:batchEmbedContents"
        requests = [
            {
                "model": model_path,
                "content": {"parts": [{"text": (t or "")[:2048]}]},
                "taskType": "SEMANTIC_SIMILARITY",
                "outputDimensionality": 768,
            }
            for t in texts
        ]
        payload = {"requests": requests}
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    endpoint, params={"key": settings.GEMINI_API_KEY}, json=payload
                )
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 4:
                if resp.status_code == 429:
                    body = resp.text
                    if any(p in body.lower() for p in ("quota", "exhausted", "billing")):
                        key_manager.mark_quota_exhausted("gemini_embed", body[:200])
                        raise RuntimeError(f"Gemini batch embed quota exhausted: {body[:200]}")
                    key_manager.mark_rate_limited("gemini_embed")
                await _sleep_before_retry(resp, attempt)
                continue
            if resp.status_code >= 400:
                log.warning(f"batchEmbedContents failed ({resp.status_code}); falling back to sequential")
                out: list[list[float]] = []
                for t in texts:
                    try:
                        out.append(await self.embed(t))
                    except Exception:
                        out.append([])
                return out
            data = resp.json()
            return [item.get("values", []) for item in data.get("embeddings", [])]
        return []

    async def describe_image(self, image_bytes: bytes, context: str = "") -> str:
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
                if resp.status_code == 429:
                    body = resp.text
                    if any(p in body.lower() for p in ("quota", "exhausted", "billing")):
                        key_manager.mark_quota_exhausted("gemini_vision", body[:200])
                        raise RuntimeError(f"Gemini vision quota exhausted")
                    key_manager.mark_rate_limited("gemini_vision")
                await _sleep_before_retry(resp, attempt)
                continue
            resp.raise_for_status()
            result = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            return "" if result == "NO_CHART" else result
        return ""


# ── OpenAI Client (vision + generation) ───────────────────────────────────────

class OpenAIClient:
    """OpenAI GPT-4o-mini — vision, generation, and embedding fallback."""

    def __init__(self, model: str | None = None, max_tokens: int | None = None):
        self.model = model or settings.OPENAI_VISION_MODEL
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        self._base = settings.OPENAI_BASE_URL

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }

    async def generate(self, prompt: str) -> str:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": settings.LLM_TEMPERATURE,
        }
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{self._base}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 4:
                await _sleep_before_retry(resp, attempt)
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(str(exc)) from exc
            return resp.json()["choices"][0]["message"]["content"]

    async def describe_image(self, image_bytes: bytes, context: str = "") -> str:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        prompt = (
            f"Context: {context}\n\n" if context else ""
        ) + (
            "You are an expert at reading statistical charts and graphs in textbooks. "
            "Describe what the chart shows: type of visualisation, axis labels, key values, "
            "trends, and what statistical concept it demonstrates. "
            "If no meaningful chart is present, respond exactly: NO_CHART"
        )
        payload = {
            "model": settings.OPENAI_VISION_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            "max_tokens": 500,
        }
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{self._base}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 4:
                await _sleep_before_retry(resp, attempt)
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(str(exc)) from exc
            text = resp.json()["choices"][0]["message"]["content"].strip()
            return "" if text == "NO_CHART" else text
        return ""

    async def embed(self, text: str) -> list[float]:
        return await _openai_embed_client.embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await _openai_embed_client.embed_batch(texts)


# ── Anthropic Client ───────────────────────────────────────────────────────────

class AnthropicClient:
    """Anthropic Claude — vision, generation fallback."""

    def __init__(self, model: str | None = None, max_tokens: int | None = None):
        self.model = model or settings.ANTHROPIC_VISION_MODEL
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS

    async def generate(self, prompt: str) -> str:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        for attempt in range(5):
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
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 4:
                await _sleep_before_retry(resp, attempt)
                continue
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]

    async def describe_image(self, image_bytes: bytes, context: str = "") -> str:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        prompt = (
            f"Context: {context}\n\n" if context else ""
        ) + (
            "Describe this statistical chart or graph: type of visualisation, axis labels, "
            "key values, trends, and what statistical concept it demonstrates. "
            "If no meaningful chart is present, respond exactly: NO_CHART"
        )
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": settings.ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": settings.ANTHROPIC_VISION_MODEL,
                        "max_tokens": 500,
                        "messages": [{"role": "user", "content": [
                            {"type": "image", "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            }},
                            {"type": "text", "text": prompt},
                        ]}],
                    },
                )
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 4:
                await _sleep_before_retry(resp, attempt)
                continue
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"].strip()
            return "" if text == "NO_CHART" else text
        return ""

    async def embed(self, text: str) -> list[float]:
        return await _openai_embed_client.embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await _openai_embed_client.embed_batch(texts)


# ── Groq Client ────────────────────────────────────────────────────────────────

class GroqClient:
    """Groq Chat Completions — question generation, math extraction."""

    def __init__(self, model: str | None = None, max_tokens: int | None = None):
        self.model = model or settings.GROQ_GENERATION_MODEL
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        self._base = settings.GROQ_BASE_URL

    async def generate(self, prompt: str) -> str:
        if not settings.GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY is not set.")
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": settings.LLM_TEMPERATURE,
        }
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{self._base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.GROQ_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if resp.status_code == 429 and attempt < 4:
                await _sleep_before_retry(resp, attempt)
                continue
            if resp.status_code in {500, 502, 503, 504} and attempt < 4:
                await asyncio.sleep(5.0 * (attempt + 1))
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(str(exc)) from exc
            return resp.json()["choices"][0]["message"]["content"]

    async def describe_image(self, image_bytes: bytes, context: str = "") -> str:
        if not settings.GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY is not set.")
        model = getattr(settings, "GROQ_MATH_MODEL", "llama-3.2-11b-vision-preview")
        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": context or "Describe this image."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img}"}},
            ]}],
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
        }
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{self._base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.GROQ_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if resp.status_code == 429 and attempt < 4:
                await _sleep_before_retry(resp, attempt)
                continue
            if resp.status_code in {500, 502, 503, 504} and attempt < 4:
                await asyncio.sleep(5.0 * (attempt + 1))
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(str(exc)) from exc
            return resp.json()["choices"][0]["message"]["content"]

    async def embed(self, text: str) -> list[float]:
        return await smart_embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await smart_embed_batch(texts)


# ── Mistral Client ─────────────────────────────────────────────────────────────

class MistralClient:
    """Mistral AI — answer marking."""

    def __init__(self, model: str | None = None, max_tokens: int | None = None):
        self.model = model or settings.MISTRAL_MARKING_MODEL
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        self._base = settings.MISTRAL_BASE_URL

    async def generate(self, prompt: str) -> str:
        if not settings.MISTRAL_API_KEY:
            raise RuntimeError("MISTRAL_API_KEY is not set.")
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": settings.LLM_TEMPERATURE,
        }
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{self._base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.MISTRAL_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if resp.status_code == 429 and attempt < 4:
                await _sleep_before_retry(resp, attempt)
                continue
            if resp.status_code in {500, 502, 503, 504} and attempt < 4:
                await asyncio.sleep(5.0 * (attempt + 1))
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(str(exc)) from exc
            return resp.json()["choices"][0]["message"]["content"]

    async def embed(self, text: str) -> list[float]:
        return await smart_embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await smart_embed_batch(texts)


# ── Smart embedding with automatic Gemini → OpenAI fallback ───────────────────

_gemini_client_singleton = GeminiClient(model="gemini-2.5-flash", max_tokens=256)
_openai_embed_client = OpenAIEmbeddingClient()
_openai_client_singleton: OpenAIClient | None = None
_anthropic_client_singleton: AnthropicClient | None = None


def _openai_client() -> OpenAIClient:
    global _openai_client_singleton
    if _openai_client_singleton is None:
        _openai_client_singleton = OpenAIClient()
    return _openai_client_singleton


def _anthropic_client() -> AnthropicClient:
    global _anthropic_client_singleton
    if _anthropic_client_singleton is None:
        _anthropic_client_singleton = AnthropicClient()
    return _anthropic_client_singleton


async def smart_embed(text: str) -> list[float]:
    """
    768-dim embedding with automatic fallback:
      1. Gemini gemini-embedding-001  (free, 768-dim)
      2. OpenAI text-embedding-3-small (paid, 768-dim via dimensions=768)
    Both produce 768-dim vectors so the MongoDB index works with either.
    """
    async def _try(provider: str) -> list[float]:
        if provider == "gemini_embed":
            if not settings.GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY not set")
            return await _gemini_client_singleton.embed(text)
        if provider == "openai_embed":
            if not settings.OPENAI_API_KEY:
                raise RuntimeError("OPENAI_API_KEY not set")
            return await _openai_embed_client.embed(text)
        raise RuntimeError(f"Unknown embed provider: {provider}")

    providers = []
    if settings.GEMINI_API_KEY:
        providers.append("gemini_embed")
    if settings.OPENAI_API_KEY:
        providers.append("openai_embed")
    if not providers:
        raise RuntimeError("No embedding provider available — set GEMINI_API_KEY or OPENAI_API_KEY")

    return await key_manager.with_fallback("embedding", providers, _try)


async def smart_embed_batch(texts: list[str]) -> list[list[float]]:
    """Batch embedding with Gemini → OpenAI fallback."""
    if not texts:
        return []

    async def _try(provider: str) -> list[list[float]]:
        if provider == "gemini_embed":
            if not settings.GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY not set")
            return await _gemini_client_singleton.embed_batch(texts)
        if provider == "openai_embed":
            if not settings.OPENAI_API_KEY:
                raise RuntimeError("OPENAI_API_KEY not set")
            return await _openai_embed_client.embed_batch(texts)
        raise RuntimeError(f"Unknown embed provider: {provider}")

    providers = []
    if settings.GEMINI_API_KEY:
        providers.append("gemini_embed")
    if settings.OPENAI_API_KEY:
        providers.append("openai_embed")
    if not providers:
        raise RuntimeError("No embedding provider available")

    return await key_manager.with_fallback("embedding_batch", providers, _try)


async def smart_describe_image(image_bytes: bytes, context: str = "") -> str:
    """
    Chart/image description with automatic fallback:
      1. Gemini 2.5 Flash Vision  (free)
      2. OpenAI GPT-4o-mini       (paid)
      3. Anthropic Claude Haiku   (paid)
    """
    async def _try(provider: str) -> str:
        if provider == "gemini_vision":
            return await _gemini_client_singleton.describe_image(image_bytes, context)
        if provider == "openai_vision":
            return await _openai_client().describe_image(image_bytes, context)
        if provider == "anthropic_vision":
            return await _anthropic_client().describe_image(image_bytes, context)
        raise RuntimeError(f"Unknown vision provider: {provider}")

    providers = []
    if settings.GEMINI_API_KEY:
        providers.append("gemini_vision")
    if settings.OPENAI_API_KEY:
        providers.append("openai_vision")
    if settings.ANTHROPIC_API_KEY:
        providers.append("anthropic_vision")
    if not providers:
        return ""

    try:
        return await key_manager.with_fallback("vision", providers, _try)
    except Exception as exc:
        log.warning(f"All vision providers failed: {exc}")
        return ""


# ── Module-level singletons ────────────────────────────────────────────────────

# slm_service: embeddings + vision — wraps smart_embed* via a thin adapter
class _SmartEmbedAdapter:
    """Adapter that exposes .embed() / .embed_batch() / .describe_image() using the
    smart rotation functions so all callers get automatic Gemini→OpenAI fallback."""

    async def embed(self, text: str) -> list[float]:
        return await smart_embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await smart_embed_batch(texts)

    async def describe_image(self, image_bytes: bytes, context: str = "") -> str:
        return await smart_describe_image(image_bytes, context)

    async def generate(self, prompt: str) -> str:
        return await _gemini_client_singleton.generate(prompt)


slm_service = _SmartEmbedAdapter()
llm_service = slm_service  # legacy alias


def _build_online_client():
    if not settings.ONLINE_LLM_ENABLED:
        return None
    if settings.ONLINE_LLM_PROVIDER == "mistral" and settings.MISTRAL_API_KEY:
        return MistralClient(model=settings.MISTRAL_MARKING_MODEL, max_tokens=settings.LLM_MAX_TOKENS)
    if settings.ONLINE_LLM_PROVIDER == "groq" and settings.GROQ_API_KEY:
        return GroqClient(model=settings.GROQ_GENERATION_MODEL, max_tokens=settings.LLM_MAX_TOKENS)
    if settings.ONLINE_LLM_PROVIDER == "anthropic" and settings.ANTHROPIC_API_KEY:
        return AnthropicClient(model=settings.ONLINE_LLM_MODEL)
    if settings.ONLINE_LLM_PROVIDER == "openai" and settings.OPENAI_API_KEY:
        return OpenAIClient(model=settings.ONLINE_LLM_MODEL)
    if settings.GEMINI_API_KEY:
        return GeminiClient(model=settings.ONLINE_LLM_MODEL)
    return None


online_service = _build_online_client()


def _build_generation_client():
    if settings.GENERATION_LLM_PROVIDER == "groq" and settings.GROQ_API_KEY:
        return GroqClient(model=settings.GROQ_GENERATION_MODEL, max_tokens=settings.GENERATION_MAX_TOKENS)
    if settings.GENERATION_LLM_PROVIDER == "anthropic" and settings.ANTHROPIC_API_KEY:
        return AnthropicClient(model=settings.GENERATION_LLM_MODEL, max_tokens=settings.GENERATION_MAX_TOKENS)
    if settings.GENERATION_LLM_PROVIDER == "openai" and settings.OPENAI_API_KEY:
        return OpenAIClient(model=settings.GENERATION_LLM_MODEL, max_tokens=settings.GENERATION_MAX_TOKENS)
    if settings.GEMINI_API_KEY:
        return GeminiClient(model=settings.GENERATION_LLM_MODEL, max_tokens=settings.GENERATION_MAX_TOKENS)
    return slm_service


generation_service = _build_generation_client()
