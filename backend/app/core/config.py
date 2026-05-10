from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # ── Database ─────────────────────────────────────────────────────────────
    DATABASE_URL: str

    # ── Auth ─────────────────────────────────────────────────────────────────
    SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 30
    SESSION_TIMEOUT_MINUTES: int = 30
    MAX_FAILED_LOGIN_ATTEMPTS: int = 3
    LOCKOUT_DURATION_MINUTES: int = 5

    # ── Celery ───────────────────────────────────────────────────────────────
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    # ── Offline LLM (Ollama) ─────────────────────────────────────────────────
    OLLAMA_BASE_URL: str = "http://llm:11434"
    LLM_MODEL_NAME: str = "llama3"          # Tier-3 offline marker
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_TOKENS: int = 1024

    # ── SLM (Small Language Model via Ollama) ────────────────────────────────
    SLM_MODEL_NAME: str = "phi3:mini"       # Tier-1 pre-scorer (phi3-mini or tinyllama)
    SLM_TEMPERATURE: float = 0.0            # Deterministic for scoring
    SLM_MAX_TOKENS: int = 256

    # ── Online LLM (optional) ────────────────────────────────────────────────
    ONLINE_LLM_ENABLED: bool = False        # Set True to activate online fallback
    ONLINE_LLM_PROVIDER: str = "anthropic"  # "anthropic" | "openai"
    ONLINE_LLM_MODEL: str = "claude-sonnet-4-20250514"
    ANTHROPIC_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None

    # ── RAG / Embeddings ─────────────────────────────────────────────────────
    EMBEDDING_MODEL: str = "nomic-embed-text"
    SIMILARITY_THRESHOLD: float = 0.75
    TOP_K_RETRIEVAL: int = 5
    TOP_K_WIDE_RETRIEVAL: int = 10          # Used on LOW-confidence path

    # ── Confidence Router thresholds ─────────────────────────────────────────
    # HIGH  >= CONFIDENCE_HIGH  → SLM mark accepted, no LLM call
    # MID   >= CONFIDENCE_MID   → RAG + offline LLM
    # LOW   <  CONFIDENCE_MID   → RAG wide + online LLM (or flag-only)
    CONFIDENCE_HIGH: float = 0.85
    CONFIDENCE_MID: float = 0.55

    # ── Application ──────────────────────────────────────────────────────────
    BATCH_SIZE_LIMIT: int = 50
    BACKUP_RETENTION_DAYS: int = 30
    UPLOAD_MAX_SIZE_MB: int = 25

    class Config:
        env_file = ".env"


settings = Settings()
