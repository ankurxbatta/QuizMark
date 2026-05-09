import httpx
from app.core.config import settings


class LLMService:
    """Abstraction layer for the local Ollama LLM adapter."""

    def __init__(self):
        self.base_url = settings.OLLAMA_BASE_URL
        self.model = settings.LLM_MODEL_NAME
        self.temperature = settings.LLM_TEMPERATURE

    async def generate(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self.base_url}/api/generate", json=payload)
            response.raise_for_status()
            return response.json()["response"]

    async def embed(self, text: str) -> list[float]:
        payload = {"model": settings.EMBEDDING_MODEL, "prompt": text}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{self.base_url}/api/embeddings", json=payload)
            response.raise_for_status()
            return response.json()["embedding"]


llm_service = LLMService()
