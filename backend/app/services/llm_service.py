"""
llm_service.py — Unified LLM adapter: Gemini · OpenAI · Anthropic only.

Provider roles:
  Gemini    → embeddings (768-dim, free quota)
  OpenAI    → embeddings fallback (768-dim), vision, math, generation, marking
  Anthropic → fallback for vision, generation, marking

Fallback chains:
  Embeddings : Gemini → OpenAI text-embedding-3-small (both 768-dim)
  Vision     : Gemini → OpenAI gpt-4o-mini → Anthropic claude-haiku
  Generation : OpenAI gpt-4o-mini → Anthropic claude-haiku → Gemini
  Marking    : OpenAI gpt-4o-mini → Anthropic claude-haiku → Gemini

All clients share: .generate(prompt) → str
                   .embed(text) → list[float]
                   .describe_image(bytes, ctx) → str
"""
from __future__ import annotations

import asyncio
import base64
import logging

import httpx
from app.core.config import settings
from app.services.api_key_manager import key_manager

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


async def _retry_sleep(resp: httpx.Response, attempt: int) -> None:
    try:
        delay = float(resp.headers.get("retry-after") or 0)
    except ValueError:
        delay = 0.0
    await asyncio.sleep(max(delay, 5.0 * (attempt + 1)))


# ── OpenAI ────────────────────────────────────────────────────────────────────

class OpenAIClient:
    """OpenAI gpt-4o-mini — generation, vision, marking. text-embedding-3-small for embeds."""

    def __init__(self, model: str | None = None, max_tokens: int | None = None):
        self.model = model or settings.OPENAI_GENERATION_MODEL
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS

    def _hdrs(self) -> dict:
        return {"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"}

    async def generate(self, prompt: str) -> str:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY not set")
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post(f"{settings.OPENAI_BASE_URL}/chat/completions",
                    headers=self._hdrs(),
                    json={"model": self.model, "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": self.max_tokens, "temperature": settings.LLM_TEMPERATURE})
            if r.status_code in {408, 429, 431, 500, 502, 503, 504} and attempt < 4:
                await _retry_sleep(r, attempt); continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    async def describe_image(self, image_bytes: bytes, context: str = "") -> str:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY not set")
        b64 = base64.b64encode(image_bytes).decode()
        prompt = (f"Context: {context}\n\n" if context else "") + (
            "Describe this statistical chart or graph: type of visualisation, axis labels, "
            "key values, trends, and what statistical concept it demonstrates. "
            "If no meaningful chart, respond exactly: NO_CHART")
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post(f"{settings.OPENAI_BASE_URL}/chat/completions",
                    headers=self._hdrs(),
                    json={"model": settings.OPENAI_VISION_MODEL,
                          "messages": [{"role": "user", "content": [
                              {"type": "text", "text": prompt},
                              {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                          ]}], "max_tokens": 500})
            if r.status_code in {408, 429, 431, 500, 502, 503, 504} and attempt < 4:
                await _retry_sleep(r, attempt); continue
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            return "" if text == "NO_CHART" else text

    async def embed(self, text: str) -> list[float]:
        return await _openai_embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await _openai_embed_batch(texts)


# ── Anthropic ─────────────────────────────────────────────────────────────────

class AnthropicClient:
    """Anthropic Claude Haiku — fallback for generation, vision, marking."""

    def __init__(self, model: str | None = None, max_tokens: int | None = None):
        self.model = model or settings.ANTHROPIC_GENERATION_MODEL
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS

    def _hdrs(self) -> dict:
        return {"x-api-key": settings.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01", "content-type": "application/json"}

    async def generate(self, prompt: str) -> str:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post("https://api.anthropic.com/v1/messages",
                    headers=self._hdrs(),
                    json={"model": self.model, "max_tokens": self.max_tokens,
                          "messages": [{"role": "user", "content": prompt}]})
            if r.status_code in {408, 429, 431, 500, 502, 503, 504} and attempt < 4:
                await _retry_sleep(r, attempt); continue
            r.raise_for_status()
            return r.json()["content"][0]["text"]

    async def describe_image(self, image_bytes: bytes, context: str = "") -> str:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        b64 = base64.b64encode(image_bytes).decode()
        prompt = (f"Context: {context}\n\n" if context else "") + (
            "Describe this statistical chart: type, axis labels, key values, trends, "
            "and what statistical concept it demonstrates. If no chart, respond: NO_CHART")
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post("https://api.anthropic.com/v1/messages",
                    headers=self._hdrs(),
                    json={"model": settings.ANTHROPIC_VISION_MODEL, "max_tokens": 500,
                          "messages": [{"role": "user", "content": [
                              {"type": "image", "source": {"type": "base64",
                               "media_type": "image/png", "data": b64}},
                              {"type": "text", "text": prompt},
                          ]}]})
            if r.status_code in {408, 429, 431, 500, 502, 503, 504} and attempt < 4:
                await _retry_sleep(r, attempt); continue
            r.raise_for_status()
            text = r.json()["content"][0]["text"].strip()
            return "" if text == "NO_CHART" else text

    async def embed(self, text: str) -> list[float]:
        return await smart_embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await smart_embed_batch(texts)


# ── Gemini ────────────────────────────────────────────────────────────────────

class GeminiClient:
    """Gemini — embeddings primary. Generation fallback only."""

    def __init__(self, model: str | None = None, max_tokens: int | None = None):
        self.model = (model or "gemini-2.5-flash").removeprefix("models/")
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        self._base = settings.GEMINI_BASE_URL

    async def generate(self, prompt: str) -> str:
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY not set")
        payload = {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"temperature": settings.LLM_TEMPERATURE,
                                        "maxOutputTokens": self.max_tokens,
                                        "thinkingConfig": {"thinkingBudget": 0}}}
        for attempt in range(5):
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post(f"{self._base}/models/{self.model}:generateContent",
                    params={"key": settings.GEMINI_API_KEY}, json=payload)
            if r.status_code in {408, 429, 431, 500, 502, 503, 504} and attempt < 4:
                if r.status_code == 429:
                    key_manager.mark_quota_exhausted("gemini", r.text[:200])
                await _retry_sleep(r, attempt); continue
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    async def embed(self, text: str) -> list[float]:
        return await smart_embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await smart_embed_batch(texts)


# ── OpenAI embedding helpers (used by all clients) ────────────────────────────

async def _openai_embed(text: str) -> list[float]:
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")
    for attempt in range(5):
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{settings.OPENAI_BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={"model": settings.OPENAI_EMBEDDING_MODEL, "input": text[:8191], "dimensions": 768})
        if r.status_code in {408, 429, 431, 500, 502, 503, 504} and attempt < 4:
            await _retry_sleep(r, attempt); continue
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]
    return []


async def _openai_embed_batch(texts: list[str]) -> list[list[float]]:
    if not texts or not settings.OPENAI_API_KEY:
        return [[] for _ in texts]
    for attempt in range(5):
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{settings.OPENAI_BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={"model": settings.OPENAI_EMBEDDING_MODEL,
                      "input": [t[:8191] for t in texts], "dimensions": 768})
        if r.status_code in {408, 429, 431, 500, 502, 503, 504} and attempt < 4:
            await _retry_sleep(r, attempt); continue
        r.raise_for_status()
        items = sorted(r.json()["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in items]
    return [[] for _ in texts]


async def _gemini_embed(text: str) -> list[float]:
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    model = settings.GEMINI_EMBEDDING_MODEL
    for attempt in range(5):
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{settings.GEMINI_BASE_URL}/models/{model}:embedContent",
                params={"key": settings.GEMINI_API_KEY},
                json={"model": f"models/{model}", "content": {"parts": [{"text": text[:2048]}]},
                      "taskType": "SEMANTIC_SIMILARITY", "outputDimensionality": 768})
        if r.status_code in {408, 429, 431, 500, 502, 503, 504} and attempt < 4:
            # A 429 here is almost always a transient per-minute rate limit
            # (free tier), not a daily/quota exhaustion. Use a short rate-limit
            # cooldown and retry with backoff instead of benching the provider
            # for an hour — otherwise a single burst sidelines the free embedder
            # for the rest of a large-book ingestion.
            if r.status_code == 429:
                key_manager.mark_rate_limited("gemini_embed")
            await _retry_sleep(r, attempt); continue
        r.raise_for_status()
        key_manager.mark_success("gemini_embed")
        return r.json()["embedding"]["values"]
    return []


async def _gemini_embed_batch(texts: list[str]) -> list[list[float]]:
    if not texts or not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    model_path = f"models/{settings.GEMINI_EMBEDDING_MODEL}"
    requests = [{"model": model_path, "content": {"parts": [{"text": (t or "")[:2048]}]},
                 "taskType": "SEMANTIC_SIMILARITY", "outputDimensionality": 768} for t in texts]
    for attempt in range(5):
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{settings.GEMINI_BASE_URL}/{model_path}:batchEmbedContents",
                params={"key": settings.GEMINI_API_KEY}, json={"requests": requests})
        if r.status_code == 429:
            # Transient per-minute rate limit — short cooldown + retry with
            # backoff, then fall through to the OpenAI embedder. Do NOT bench
            # Gemini for an hour on a burst (see _gemini_embed above).
            key_manager.mark_rate_limited("gemini_embed")
            if attempt < 4:
                await _retry_sleep(r, attempt); continue
            raise RuntimeError("Gemini batch embed rate-limited")
        if r.status_code in {500, 502, 503, 504} and attempt < 4:
            await _retry_sleep(r, attempt); continue
        if r.status_code >= 400:
            raise RuntimeError(f"Gemini batch embed {r.status_code}")
        key_manager.mark_success("gemini_embed")
        return [item.get("values", []) for item in r.json().get("embeddings", [])]
    return [[] for _ in texts]


# ── Smart embedding: Gemini → OpenAI (both 768-dim) ───────────────────────────

async def smart_embed(text: str) -> list[float]:
    async def _try(provider: str) -> list[float]:
        if provider == "gemini_embed":
            return await _gemini_embed(text)
        return await _openai_embed(text)
    providers = []
    if settings.GEMINI_API_KEY and key_manager.is_available("gemini_embed"):
        providers.append("gemini_embed")
    if settings.OPENAI_API_KEY:
        providers.append("openai_embed")
    if not providers:
        raise RuntimeError("No embedding provider available")
    return await key_manager.with_fallback("embedding", providers, _try)


async def smart_embed_batch(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    async def _try(provider: str) -> list[list[float]]:
        if provider == "gemini_embed":
            return await _gemini_embed_batch(texts)
        return await _openai_embed_batch(texts)
    providers = []
    if settings.GEMINI_API_KEY and key_manager.is_available("gemini_embed"):
        providers.append("gemini_embed")
    if settings.OPENAI_API_KEY:
        providers.append("openai_embed")
    if not providers:
        raise RuntimeError("No embedding provider available")
    return await key_manager.with_fallback("embedding_batch", providers, _try)


# ── Image generation: Gemini → OpenAI ──────────────────────────────────────────

async def _gemini_generate_image(prompt: str) -> bytes:
    """Gemini image model via the Generative Language API → PNG bytes."""
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    model = settings.GEMINI_IMAGE_MODEL.removeprefix("models/")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    for attempt in range(5):
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                f"{settings.GEMINI_BASE_URL}/models/{model}:generateContent",
                params={"key": settings.GEMINI_API_KEY}, json=payload,
            )
        if r.status_code in {408, 429, 431, 500, 502, 503, 504} and attempt < 4:
            if r.status_code == 429:
                key_manager.mark_quota_exhausted("gemini", r.text[:200])
            await _retry_sleep(r, attempt); continue
        r.raise_for_status()
        candidates = r.json().get("candidates") or []
        for cand in candidates:
            for part in (cand.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        raise RuntimeError("Gemini returned no image data")
    raise RuntimeError("Gemini image generation exhausted retries")


async def _openai_generate_image(prompt: str) -> bytes:
    """OpenAI images endpoint → PNG bytes (gpt-image-1 returns b64_json)."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")
    for attempt in range(5):
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{settings.OPENAI_BASE_URL}/images/generations",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": settings.OPENAI_IMAGE_MODEL, "prompt": prompt,
                      "size": "1024x1024"})
        if r.status_code in {408, 429, 431, 500, 502, 503, 504} and attempt < 4:
            await _retry_sleep(r, attempt); continue
        r.raise_for_status()
        data = r.json().get("data") or []
        if data and data[0].get("b64_json"):
            return base64.b64decode(data[0]["b64_json"])
        raise RuntimeError("OpenAI returned no image data")
    raise RuntimeError("OpenAI image generation exhausted retries")


async def generate_image(prompt: str) -> bytes:
    """Generate an image from a text prompt with provider fallback.

    Primary provider is settings.IMAGE_GEN_PROVIDER (default "gemini"); the
    other provider is tried on failure. Raises if every provider fails — callers
    wrap this in try/except and degrade cleanly.
    """
    primary = (settings.IMAGE_GEN_PROVIDER or "gemini").lower()
    order = ["gemini", "openai"] if primary == "gemini" else ["openai", "gemini"]
    last_exc: Exception | None = None
    for provider in order:
        try:
            if provider == "gemini":
                png = await _gemini_generate_image(prompt)
            else:
                png = await _openai_generate_image(prompt)
            if png:
                return png
        except Exception as exc:
            log.warning(f"[image-gen] {provider} failed: {exc}")
            last_exc = exc
    raise RuntimeError(f"All image providers failed: {last_exc}")


async def smart_describe_image(image_bytes: bytes, context: str = "") -> str:
    _openai = OpenAIClient()
    _anthropic = AnthropicClient()
    _gemini = GeminiClient()
    for client, name in [(_openai, "openai_vision"), (_anthropic, "anthropic_vision"), (_gemini, "gemini_vision")]:
        if not key_manager.is_available(name):
            continue
        try:
            result = await client.describe_image(image_bytes, context)
            key_manager.mark_success(name)
            return result
        except Exception as exc:
            log.warning(f"[vision] {name} failed: {exc}")
            if "429" in str(exc):
                key_manager.mark_rate_limited(name)
    return ""


# ── Adapter that exposes the smart routing behind a single interface ───────────

class _SmartAdapter:
    async def embed(self, text: str) -> list[float]:
        return await smart_embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await smart_embed_batch(texts)

    async def describe_image(self, image_bytes: bytes, context: str = "") -> str:
        return await smart_describe_image(image_bytes, context)

    async def generate(self, prompt: str) -> str:
        # Generation: OpenAI → Anthropic → Gemini
        for client in [OpenAIClient(), AnthropicClient(), GeminiClient()]:
            try:
                return await client.generate(prompt)
            except Exception:
                pass
        raise RuntimeError("All generation providers failed")


# ── Module-level singletons ────────────────────────────────────────────────────

slm_service = _SmartAdapter()
llm_service = slm_service   # legacy alias


def _build_online_client():
    """Answer marking: OpenAI → Anthropic → Gemini."""
    if not settings.ONLINE_LLM_ENABLED:
        return None
    if settings.OPENAI_API_KEY:
        return OpenAIClient(model=settings.OPENAI_MARKING_MODEL, max_tokens=settings.LLM_MAX_TOKENS)
    if settings.ANTHROPIC_API_KEY:
        return AnthropicClient(model=settings.ANTHROPIC_MARKING_MODEL, max_tokens=settings.LLM_MAX_TOKENS)
    if settings.GEMINI_API_KEY:
        return GeminiClient(max_tokens=settings.LLM_MAX_TOKENS)
    return None


class _FallbackGenerationClient:
    """Question generation with provider rotation: OpenAI → Anthropic → Gemini.

    Unlike a single static client picked at startup by key *presence*, every
    call routes through key_manager.with_fallback, so an OpenAI outage (e.g. a
    zero-credit 429 insufficient_quota) rolls over to Anthropic, then Gemini —
    matching the documented chain and respecting per-provider cooldowns.
    Per-provider generation models and GENERATION_MAX_TOKENS are preserved.
    """

    def __init__(self) -> None:
        self._clients: dict = {}
        if settings.OPENAI_API_KEY:
            self._clients["openai_generation"] = OpenAIClient(
                model=settings.OPENAI_GENERATION_MODEL, max_tokens=settings.GENERATION_MAX_TOKENS)
        if settings.ANTHROPIC_API_KEY:
            self._clients["anthropic"] = AnthropicClient(
                model=settings.ANTHROPIC_GENERATION_MODEL, max_tokens=settings.GENERATION_MAX_TOKENS)
        if settings.GEMINI_API_KEY:
            self._clients["gemini"] = GeminiClient(max_tokens=settings.GENERATION_MAX_TOKENS)

    async def generate(self, prompt: str) -> str:
        providers = [p for p in ("openai_generation", "anthropic", "gemini") if p in self._clients]
        if not providers:
            return await slm_service.generate(prompt)

        async def _try(provider: str) -> str:
            return await self._clients[provider].generate(prompt)

        return await key_manager.with_fallback("generation", providers, _try)


def _build_generation_client():
    """Question generation: OpenAI → Anthropic → Gemini (with runtime fallback)."""
    return _FallbackGenerationClient()


online_service = _build_online_client()
generation_service = _build_generation_client()
