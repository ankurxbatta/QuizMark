from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import Optional, List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[".env", "../../.env"],
        env_file_encoding="utf-8",
    )

    # ── Auth ─────────────────────────────────────────────────────────────────
    SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 30
    MAX_FAILED_LOGIN_ATTEMPTS: int = 3
    LOCKOUT_DURATION_MINUTES: int = 5
    ADMIN_ENABLED: bool = True
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: Optional[str] = None
    ADMIN_ROLE: str = "instructor"

    # ── CORS ─────────────────────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    # ── Celery ───────────────────────────────────────────────────────────────
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    # ── Gemini — embeddings + chart vision only ───────────────────────────────
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta"
    GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-001"  # 768-dim (matches MongoDB index)
    GEMINI_EMBEDDING_DELAY_SECONDS: float = 0.8
    GEMINI_VISION_DELAY_SECONDS: float = 1.0

    # ── Groq — question generation + math vision ──────────────────────────────
    GROQ_API_KEY: Optional[str] = None
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROQ_GENERATION_MODEL: str = "llama-3.3-70b-versatile"
    GROQ_MATH_MODEL: str = "meta-llama/llama-4-scout-17b-16e-instruct"

    # ── Mistral — answer marking ──────────────────────────────────────────────
    MISTRAL_API_KEY: Optional[str] = None
    MISTRAL_BASE_URL: str = "https://api.mistral.ai/v1"
    MISTRAL_MARKING_MODEL: str = "mistral-small-latest"

    # ── LLM settings ─────────────────────────────────────────────────────────
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_TOKENS: int = 1024

    # ── Online LLM (answer marking) ───────────────────────────────────────────
    ONLINE_LLM_ENABLED: bool = True
    ONLINE_LLM_PROVIDER: str = "groq"
    ONLINE_LLM_MODEL: str = "llama-3.3-70b-versatile"
    ANTHROPIC_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None

    # ── Generation LLM (question generation) ─────────────────────────────────
    GENERATION_LLM_ENABLED: bool = True
    GENERATION_LLM_PROVIDER: str = "groq"
    GENERATION_LLM_MODEL: str = "llama-3.3-70b-versatile"
    GENERATION_MAX_TOKENS: int = 4096

    # ── RAG ──────────────────────────────────────────────────────────────────
    SIMILARITY_THRESHOLD: float = 0.75
    TOP_K_RETRIEVAL: int = 5
    TOP_K_WIDE_RETRIEVAL: int = 10

    # ── Confidence router ────────────────────────────────────────────────────
    CONFIDENCE_HIGH: float = 0.85
    CONFIDENCE_MID: float = 0.55

    # ── MongoDB (primary data store + vector store) ───────────────────────────
    MONGODB_ENABLED: bool = True
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DB_NAME: str = "marking_tools"

    # ── PDF ingestion ─────────────────────────────────────────────────────────
    UPLOAD_MAX_SIZE_MB: int = 25
    PDF_MAX_PAGES: int = 700
    PDF_MIN_CHUNK_CHARS: int = 300
    PDF_MAX_CHUNK_CHARS: int = 3000

    # ── DeepSearch (web augmentation) ────────────────────────────────────────
    TAVILY_API_KEY: Optional[str] = None

    # ── Application ──────────────────────────────────────────────────────────
    BATCH_SIZE_LIMIT: int = 50
    BACKUP_RETENTION_DAYS: int = 30

    @field_validator("ADMIN_PASSWORD", mode="after")
    @classmethod
    def _require_admin_password(cls, v, info):
        admin_enabled = info.data.get("ADMIN_ENABLED", True)
        if admin_enabled and not v:
            raise ValueError(
                "ADMIN_PASSWORD must be set in .env when ADMIN_ENABLED=true."
            )
        return v


settings = Settings()
