from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str

    # Auth
    SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 30
    SESSION_TIMEOUT_MINUTES: int = 30
    MAX_FAILED_LOGIN_ATTEMPTS: int = 3
    LOCKOUT_DURATION_MINUTES: int = 5

    # Celery
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    # LLM
    OLLAMA_BASE_URL: str = "http://llm:11434"
    LLM_MODEL_NAME: str = "llama3"
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_TOKENS: int = 1024

    # RAG
    EMBEDDING_MODEL: str = "nomic-embed-text"
    SIMILARITY_THRESHOLD: float = 0.75
    TOP_K_RETRIEVAL: int = 5

    # App
    BATCH_SIZE_LIMIT: int = 50
    BACKUP_RETENTION_DAYS: int = 30
    UPLOAD_MAX_SIZE_MB: int = 10

    class Config:
        env_file = ".env"


settings = Settings()
