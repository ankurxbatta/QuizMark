from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    # ── Database ─────────────────────────────────────────────────────────────
    DATABASE_URL: str

    # ── Auth ─────────────────────────────────────────────────────────────────
    SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 30
    MAX_FAILED_LOGIN_ATTEMPTS: int = 3
    LOCKOUT_DURATION_MINUTES: int = 5
    ADMIN_ENABLED: bool = True
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin"
    ADMIN_ROLE: str = "instructor"

    # ── Celery ───────────────────────────────────────────────────────────────
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    # ── Offline LLM (Ollama) ─────────────────────────────────────────────────
    OLLAMA_BASE_URL: str = "http://llm:11434"
    LLM_MODEL_NAME: str = "llama3"
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_TOKENS: int = 1024

    # ── SLM ──────────────────────────────────────────────────────────────────
    SLM_MODEL_NAME: str = "phi3:mini"
    SLM_TEMPERATURE: float = 0.0
    SLM_MAX_TOKENS: int = 256

    # ── Online LLM (optional, for marking fallback) ──────────────────────────
    ONLINE_LLM_ENABLED: bool = False
    ONLINE_LLM_PROVIDER: str = "anthropic"
    ONLINE_LLM_MODEL: str = "claude-sonnet-4-20250514"
    ANTHROPIC_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta"

    # ── Generation LLM (for question generation) ─────────────────────────────
    GENERATION_LLM_ENABLED: bool = True
    GENERATION_LLM_PROVIDER: str = "anthropic"
    GENERATION_LLM_MODEL: str = "claude-sonnet-4-20250514"
    GENERATION_MAX_TOKENS: int = 4096

    # ── RAG ──────────────────────────────────────────────────────────────────
    EMBEDDING_MODEL: str = "nomic-embed-text"
    SIMILARITY_THRESHOLD: float = 0.75
    TOP_K_RETRIEVAL: int = 5
    TOP_K_WIDE_RETRIEVAL: int = 10

    # ── Confidence router ────────────────────────────────────────────────────
    CONFIDENCE_HIGH: float = 0.85
    CONFIDENCE_MID: float = 0.55

    # ── PDF ingestion ─────────────────────────────────────────────────────────
    UPLOAD_MAX_SIZE_MB: int = 25
    PDF_MAX_PAGES: int = 620          # Full textbook — up from old limit of 100
    PDF_MIN_CHUNK_CHARS: int = 300    # Discard chunks smaller than this
    PDF_MAX_CHUNK_CHARS: int = 3000   # Split chunks larger than this

    # ── Application ──────────────────────────────────────────────────────────
    BATCH_SIZE_LIMIT: int = 50
    BACKUP_RETENTION_DAYS: int = 30

settings = Settings()
