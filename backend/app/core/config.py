from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import Optional, List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[".env", "../../.env"],
        env_file_encoding="utf-8",
        # Retired settings may linger in existing .env files (e.g. the old
        # CONFIDENCE_HIGH / ONLINE_LLM_PROVIDER knobs) — never crash on them.
        extra="ignore",
    )

    # ── Environment ──────────────────────────────────────────────────────────
    ENVIRONMENT: str = "development"

    # ── Auth ─────────────────────────────────────────────────────────────────
    SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 30
    # Absolute session lifetime: /auth/refresh keeps renewing 30-min tokens
    # until the ORIGINAL login is this old, then forces a fresh login. Caps
    # how long a stolen token can be kept alive by refreshing.
    SESSION_MAX_MINUTES: int = 720
    # A low threshold (was 3) lets an attacker who only knows a *username*
    # deliberately lock that account out with a handful of bad passwords — a
    # targeted-lockout DoS that is especially dangerous for the sole admin.
    # Raised to 10 to preserve brute-force protection while making griefing
    # costlier. NOTE (auth.py owner): the fuller fix is exponential backoff on
    # repeated failures plus non-enumerating register/login responses (so an
    # attacker cannot confirm which usernames exist); those live in auth.py.
    MAX_FAILED_LOGIN_ATTEMPTS: int = 10
    # Minutes an account stays locked after MAX_FAILED_LOGIN_ATTEMPTS. Tunable
    # so ops can shorten the DoS window without lowering the attempt threshold.
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

    # ── LLM marking (subjective answers) ──────────────────────────────────────
    # false = no LLM marking at all; provider/model are chosen at startup by
    # key presence (OpenAI → Anthropic → Gemini) using *_MARKING_MODEL below.
    ONLINE_LLM_ENABLED: bool = True

    # ── Question generation ───────────────────────────────────────────────────
    # Provider rotation is runtime fallback (OpenAI → Anthropic → Gemini) using
    # the per-provider *_GENERATION_MODEL settings; only the budget lives here.
    GENERATION_MAX_TOKENS: int = 4096

    # ── RAG ──────────────────────────────────────────────────────────────────
    SIMILARITY_THRESHOLD: float = 0.75
    TOP_K_RETRIEVAL: int = 5
    TOP_K_WIDE_RETRIEVAL: int = 10

    # ── Marking confidence router (see services/pre_scorer.py) ────────────────
    # The no-LLM full-marks shortcut requires BOTH gates; CONFIDENCE_MID splits
    # the remaining answers into normal vs wide-RAG + flagged marking.
    PRESCORE_FULL_CREDIT_SEM: float = 0.92
    PRESCORE_FULL_CREDIT_KW: float = 0.60
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
    INGEST_VISION_CONCURRENCY: int = 10      # parallel vision API calls per pass (higher = faster ingest)
    ENABLE_CHUNK_VALIDATION: bool = True     # LLM math repair before DB insert
    VALIDATION_CONCURRENCY: int = 4          # parallel validation LLM calls
    GEMINI_EMBEDDING_DELAY_SECONDS: float = 0.5
    GEMINI_VISION_DELAY_SECONDS: float = 0.0
    ENABLE_VISION_EXTRACTION: bool = True
    EMBEDDING_BATCH_SIZE: int = 100

    # ── Resumable page-by-page ingestion ──────────────────────────────────────
    INGEST_PAGE_WINDOW: int = 4               # smaller window → more frequent progress/heartbeat updates
    INGEST_TIME_BUDGET_SECONDS: int = 1500

    # ── Specialist RAG indexes (MULTI_RAG_DESIGN) ──────────────────────────────
    MATH_INDEX_ENABLED: bool = True
    FIGURE_INDEX_ENABLED: bool = True
    TABLE_INDEX_ENABLED: bool = True
    EXERCISE_INDEX_ENABLED: bool = True
    INDEX_BUILD_BATCH_SIZE: int = 10          # items per enrichment LLM call
    RRF_K: int = 60                           # reciprocal-rank fusion constant
    # Phase 4 — lexical per-specialist reranking of each result list before
    # RRF fusion (deterministic, no LLM calls). ALPHA weights the original
    # vector rank vs the lexical score; 1.0 disables the lexical signal.
    RERANK_ENABLED: bool = True
    RERANK_ALPHA: float = 0.5
    EXPANSION_NEIGHBORS: int = 2              # parent chunks pulled in via cross-links

    # ── Question generation throughput ────────────────────────────────────────
    GEN_CHAPTER_CONCURRENCY: int = 5        # OpenAI/Anthropic handle higher concurrency
    DEDUP_SIMILARITY_THRESHOLD: float = 0.92
    # Drop generated questions that are near-duplicates (cosine >= threshold above)
    # of questions ALREADY stored for the same book, so separate generation runs
    # don't accumulate equivalent questions. Off → only within-run dedup applies.
    GEN_BANK_DEDUP_ENABLED: bool = True
    # When dedup/validation drops a chapter below its requested count, regenerate
    # quality replacements for up to this many extra rounds before giving up.
    GEN_TOPUP_MAX_ROUNDS: int = 3
    # Bloom's coverage rounds (gap-fill + concept-coverage) only meaningfully help
    # LARGE banks — for a tiny request you can't spread questions across 5 levels,
    # so the extra sequential LLM calls are wasted latency. Skip them when the
    # requested count is below this. Round 1 + the top-up loops still hit the count;
    # the quality gate still runs. Set to 0 to always run full coverage.
    GEN_FULL_COVERAGE_MIN_COUNT: int = 6
    # When difficulty="hard", verify each question genuinely requires multi-step
    # reasoning (≥2 chained steps / combines concepts / evaluative) and DROP the
    # ones that collapse to a single step, so the top-up loop regenerates them.
    # One cheap LLM call per hard question; runs ONLY for hard. Off → rely on the
    # prompt instruction + the deterministic _is_trivial_recall guard alone.
    GEN_HARD_VERIFY_ENABLED: bool = True
    GEN_HARD_VERIFY_CONCURRENCY: int = 3       # parallel hard-difficulty judge calls

    # ── DeepSearch refiner (repair pass BEFORE the quality gate) ──────────────
    # For each freshly generated question DeepSearch gathers evidence from every
    # index (chunks + math/figure/table), optionally the web, and runs one
    # critic-repair LLM call that completes missing pieces and corrects errors so
    # the validator keeps the question instead of dropping it. Fail-open: any
    # refiner error leaves the question unchanged.
    DEEPSEARCH_REFINE_ENABLED: bool = True
    DEEPSEARCH_CONCURRENCY: int = 3            # parallel refine LLM calls
    DEEPSEARCH_RETRIEVAL_K: int = 4            # evidence chunks per question
    # Web knowledge: inert unless TAVILY_API_KEY is set (https://tavily.com).
    DEEPSEARCH_WEB_ENABLED: bool = True
    DEEPSEARCH_WEB_MAX_RESULTS: int = 3
    TAVILY_API_KEY: Optional[str] = None

    # ── Question-quality gate (reject un-renderable / unanswerable / wrong) ───
    # A: deterministic renderability checks (no LLM). B: deepsearch answerability
    # + correctness judge (one LLM call per surviving question). Rejected
    # questions are DROPPED from the verified list so the top-up loops regenerate
    # replacements — keeping the requested count while raising quality.
    QUALITY_GATE_ENABLED: bool = True          # master switch for both A and B
    QUALITY_JUDGE_ENABLED: bool = True         # LLM answerability/correctness judge (B)
    QUALITY_JUDGE_CONCURRENCY: int = 3         # parallel judge LLM calls
    QUALITY_JUDGE_RETRIEVAL_K: int = 4         # supporting chunks fetched per judged question

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
