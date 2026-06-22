from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import Optional, List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[".env", "../../.env"],
        env_file_encoding="utf-8",
    )

    # ── Environment ──────────────────────────────────────────────────────────
    ENVIRONMENT: str = "development"

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

    # ── Gemini — embeddings only ──────────────────────────────────────────────
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta"
    GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-001"

    # ── OpenAI — primary paid provider ───────────────────────────────────────
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    OPENAI_VISION_MODEL: str = "gpt-4o-mini"
    OPENAI_GENERATION_MODEL: str = "gpt-4o-mini"
    OPENAI_MARKING_MODEL: str = "gpt-4o-mini"

    # ── Anthropic — fallback for all LLM tasks ────────────────────────────────
    ANTHROPIC_API_KEY: Optional[str] = None
    ANTHROPIC_BASE_URL: str = "https://api.anthropic.com/v1"
    ANTHROPIC_VISION_MODEL: str = "claude-haiku-4-5-20251001"
    ANTHROPIC_GENERATION_MODEL: str = "claude-haiku-4-5-20251001"
    ANTHROPIC_MARKING_MODEL: str = "claude-haiku-4-5-20251001"

    # ── LLM settings ─────────────────────────────────────────────────────────
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_TOKENS: int = 1024

    # ── Image generation (question figure assets) ────────────────────────────
    # OpenAI gpt-image-1 is the verified-working default; Gemini's preview image
    # model name churns and 404s, so it is only the fallback.
    IMAGE_GEN_ENABLED: bool = True
    IMAGE_GEN_PROVIDER: str = "openai"
    GEMINI_IMAGE_MODEL: str = "gemini-2.5-flash-image-preview"
    OPENAI_IMAGE_MODEL: str = "gpt-image-1"
    ASSET_MAX_PER_CHAPTER: int = 4

    # ── Online LLM (answer marking) ───────────────────────────────────────────
    ONLINE_LLM_ENABLED: bool = True
    ONLINE_LLM_PROVIDER: str = "openai"
    ONLINE_LLM_MODEL: str = "gpt-4o-mini"

    # ── Generation LLM (question generation) ─────────────────────────────────
    GENERATION_LLM_ENABLED: bool = True
    GENERATION_LLM_PROVIDER: str = "openai"
    GENERATION_LLM_MODEL: str = "gpt-4o-mini"
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

    # ── Ingestion chain (chunking / validation / parallelism) ─────────────────
    CHUNK_OVERLAP: int = 200                 # recursive splitter overlap (chars)
    SEMANTIC_CHUNK_RATIO: float = 0.2        # share of chunks re-split semantically
    SEMANTIC_MIN_CHARS: int = 1500           # only chunks this long are candidates
    INGEST_VISION_CONCURRENCY: int = 6       # parallel vision API calls per pass
    ENABLE_CHUNK_VALIDATION: bool = True     # LLM math repair before DB insert
    VALIDATION_CONCURRENCY: int = 4          # parallel validation LLM calls
    GEMINI_EMBEDDING_DELAY_SECONDS: float = 0.5
    GEMINI_VISION_DELAY_SECONDS: float = 0.0
    ENABLE_VISION_EXTRACTION: bool = True
    EMBEDDING_BATCH_SIZE: int = 100

    # ── Resumable page-by-page ingestion ──────────────────────────────────────
    INGEST_PAGE_WINDOW: int = 6
    INGEST_TIME_BUDGET_SECONDS: int = 1500

    # ── Specialist RAG indexes (MULTI_RAG_DESIGN) ──────────────────────────────
    MATH_INDEX_ENABLED: bool = True
    FIGURE_INDEX_ENABLED: bool = True
    TABLE_INDEX_ENABLED: bool = True
    EXERCISE_INDEX_ENABLED: bool = True
    INDEX_BUILD_BATCH_SIZE: int = 10          # items per enrichment LLM call
    RRF_K: int = 60                           # reciprocal-rank fusion constant
    EXPANSION_NEIGHBORS: int = 2              # parent chunks pulled in via cross-links

    # ── Question generation throughput ────────────────────────────────────────
    GEN_CHAPTER_CONCURRENCY: int = 5        # OpenAI/Anthropic handle higher concurrency
    DEDUP_SIMILARITY_THRESHOLD: float = 0.92

    # ── Application ──────────────────────────────────────────────────────────
    BATCH_SIZE_LIMIT: int = 50
    BACKUP_RETENTION_DAYS: int = 30

    @field_validator("ADMIN_PASSWORD", mode="after")
    @classmethod
    def _require_admin_password(cls, v, info):
        admin_enabled = info.data.get("ADMIN_ENABLED", True)
        if admin_enabled and not v:
            raise ValueError("ADMIN_PASSWORD must be set in .env when ADMIN_ENABLED=true.")
        return v


settings = Settings()
