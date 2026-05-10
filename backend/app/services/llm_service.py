"""
llm_service.py  —  Unified LLM adapter.

Exposes three clients:
  slm_service    → phi3:mini  (Tier-1 SLM pre-scorer)
  llm_service    → llama3     (Tier-3 offline LLM marker)
  online_service → Anthropic / OpenAI  (Tier-3 optional online fallback)

All share the same interface:  .generate(prompt) → str
                                .embed(text)      → list[float]
"""
import httpx
from app.core.config import settings


class OllamaClient:
    """Talks to a local Ollama model (used for both SLM and offline LLM)."""

    def __init__(self, model: str, temperature: float, max_tokens: int):
        self.base_url = settings.OLLAMA_BASE_URL
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def generate(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }
        # Use long timeout for offline generation - local models can be very slow
        timeout = 600 if self.model in ["phi3:mini", "llama3"] else 120
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{self.base_url}/api/generate", json=payload)
            resp.raise_for_status()
            return resp.json()["response"]

    async def embed(self, text: str) -> list[float]:
        payload = {"model": settings.EMBEDDING_MODEL, "prompt": text}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{self.base_url}/api/embeddings", json=payload)
            resp.raise_for_status()
            return resp.json()["embedding"]


class AnthropicClient:
    """Calls the Anthropic Messages API (Claude)."""

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
                    "model": settings.ONLINE_LLM_MODEL,
                    "max_tokens": settings.LLM_MAX_TOKENS,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]

    async def embed(self, text: str) -> list[float]:
        # Anthropic doesn't expose embeddings — fall back to local model
        return await slm_service.embed(text)


class OpenAIClient:
    """Calls the OpenAI Chat Completions API."""

    async def generate(self, prompt: str) -> str:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={
                    "model": settings.ONLINE_LLM_MODEL,
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


class GeminiClient:
    """Calls the Google Gemini Generative Language API."""

    async def generate(self, prompt: str) -> str:
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        endpoint = (
            f"{settings.GEMINI_BASE_URL}/models/"
            f"{settings.ONLINE_LLM_MODEL}:generateContent"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": settings.LLM_TEMPERATURE,
                "maxOutputTokens": settings.LLM_MAX_TOKENS,
            },
        }
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                endpoint,
                params={"key": settings.GEMINI_API_KEY},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]

    async def embed(self, text: str) -> list[float]:
        # Use local embeddings for consistency with pgvector length
        return await slm_service.embed(text)


# ── Module-level singletons ────────────────────────────────────────────────────

# Tier-1: SLM — small, fast, deterministic
slm_service = OllamaClient(
    model=settings.SLM_MODEL_NAME,
    temperature=settings.SLM_TEMPERATURE,
    max_tokens=settings.SLM_MAX_TOKENS,
)

# Tier-3 offline: full LLM
llm_service = OllamaClient(
    model=settings.LLM_MODEL_NAME,
    temperature=settings.LLM_TEMPERATURE,
    max_tokens=settings.LLM_MAX_TOKENS,
)

# Tier-3 online (optional, for marking fallback)
def _build_online_client():
    if not settings.ONLINE_LLM_ENABLED:
        return None
    if settings.ONLINE_LLM_PROVIDER == "anthropic":
        return AnthropicClient()
    if settings.ONLINE_LLM_PROVIDER == "openai":
        return OpenAIClient()
    if settings.ONLINE_LLM_PROVIDER == "gemini":
        return GeminiClient()
    return None

online_service = _build_online_client()

# Generation service (for question generation) — uses small model for memory efficiency
def _build_generation_client():
    # Use phi3:mini (2.3 GiB) instead of llama3 (4.7 GiB) to avoid memory issues
    # Generation doesn't need the largest model - phi3:mini is perfectly capable
    if not settings.GENERATION_LLM_ENABLED:
        # Use phi3:mini for generation (small but capable for structured output)
        return OllamaClient(
            model="phi3:mini",
            temperature=0.2,  # Allow some variability for question generation
            max_tokens=512,    # Reduced for faster generation on limited resources
        )
    if settings.GENERATION_LLM_PROVIDER == "anthropic":
        return AnthropicClient()
    if settings.GENERATION_LLM_PROVIDER == "openai":
        return OpenAIClient()
    if settings.GENERATION_LLM_PROVIDER == "gemini":
        return GeminiClient()
    # Default fallback: use local phi3:mini
    return OllamaClient(
        model="phi3:mini",
        temperature=0.2,
        max_tokens=2048,
    )

generation_service = _build_generation_client()
