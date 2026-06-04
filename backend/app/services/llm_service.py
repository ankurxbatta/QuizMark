"""
llm_service.py  —  Unified LLM adapter.

Provider split (all free tiers):
  Gemini  → embeddings (768-dim, matches MongoDB index) + chart/image vision
  Groq    → question generation  (llama-3.3-70b-versatile — great at long structured output)
  Mistral → answer marking       (mistral-small-latest — precise instruction-following for rubrics)

Public singletons:
  slm_service        → GeminiClient  (embeddings + vision)
  generation_service → GroqClient    (question generation)
  online_service     → MistralClient (answer marking)

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
        self.model = (model or "gemini-2.5-flash").removeprefix("models/")  # always a Gemini model
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


class GroqClient:
    """
    Calls the Groq Chat Completions API (OpenAI-compatible).
    Used for question generation and answer marking — free tier.
    """

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
                # Groq returns retry-after on rate limit
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
        b64_img = base64.b64encode(image_bytes).decode('utf-8')
        
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": context or "Describe this image."},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img}"}}
                    ]
                }
            ],
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
                import asyncio
                await asyncio.sleep(5.0 * (attempt + 1))
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(str(exc)) from exc
            return resp.json()["choices"][0]["message"]["content"]

    async def embed(self, text: str) -> list[float]:
        # Groq doesn't provide embeddings — delegate to Gemini
        return await slm_service.embed(text)


class MistralClient:
    """
    Calls the Mistral AI Chat API (OpenAI-compatible).
    Used for answer marking — precise instruction-following suits rubric scoring.
    """

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
        # Mistral embeddings exist but we use Gemini for consistency with MongoDB index
        return await slm_service.embed(text)


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


# ── Module-level singletons ────────────────────────────────────────────────────
#
# Gemini  → embeddings + chart vision  (slm_service / describe_image)
# Groq    → question generation + marking  (generation_service / online_service)

# Gemini client — used ONLY for embeddings and vision, never for text generation
slm_service = GeminiClient(
    model="gemini-2.5-flash",
    max_tokens=256,
)

# Legacy alias kept for any callers that import llm_service directly
llm_service = slm_service


def _build_online_client():
    """Answer marking client — Mistral by default, falls back through Groq → Gemini."""
    if not settings.ONLINE_LLM_ENABLED:
        return None
    if settings.ONLINE_LLM_PROVIDER == "mistral" and settings.MISTRAL_API_KEY:
        return MistralClient(
            model=settings.MISTRAL_MARKING_MODEL,
            max_tokens=settings.LLM_MAX_TOKENS,
        )
    if settings.ONLINE_LLM_PROVIDER == "groq" and settings.GROQ_API_KEY:
        return GroqClient(
            model=settings.GROQ_GENERATION_MODEL,
            max_tokens=settings.LLM_MAX_TOKENS,
        )
    if settings.ONLINE_LLM_PROVIDER == "anthropic" and settings.ANTHROPIC_API_KEY:
        return AnthropicClient(model=settings.ONLINE_LLM_MODEL)
    if settings.ONLINE_LLM_PROVIDER == "openai" and settings.OPENAI_API_KEY:
        return OpenAIClient(model=settings.ONLINE_LLM_MODEL)
    if settings.GEMINI_API_KEY:
        return GeminiClient(model=settings.ONLINE_LLM_MODEL)
    return None


online_service = _build_online_client()


def _build_generation_client():
    """Question generation client — Groq by default, falls back to Gemini."""
    if settings.GENERATION_LLM_PROVIDER == "groq" and settings.GROQ_API_KEY:
        return GroqClient(
            model=settings.GROQ_GENERATION_MODEL,
            max_tokens=settings.GENERATION_MAX_TOKENS,
        )
    if settings.GENERATION_LLM_PROVIDER == "anthropic" and settings.ANTHROPIC_API_KEY:
        return AnthropicClient(
            model=settings.GENERATION_LLM_MODEL,
            max_tokens=settings.GENERATION_MAX_TOKENS,
        )
    if settings.GENERATION_LLM_PROVIDER == "openai" and settings.OPENAI_API_KEY:
        return OpenAIClient(model=settings.GENERATION_LLM_MODEL)
    # Fallback: Gemini
    if settings.GEMINI_API_KEY:
        return GeminiClient(
            model=settings.GENERATION_LLM_MODEL,
            max_tokens=settings.GENERATION_MAX_TOKENS,
        )
    return slm_service


generation_service = _build_generation_client()
