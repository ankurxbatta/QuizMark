from celery import Celery
from kombu import Queue, Exchange
from app.core.config import settings

celery_app = Celery(
    "quiz_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.marking_tasks",
        "app.tasks.ingest_tasks",
        "app.tasks.clean_tasks",
        "app.tasks.deepsearch_tasks",
    ],
)

# ── Queue definitions ──────────────────────────────────────────────────────────
# Each queue maps to a specialised worker container. Workers only subscribe to
# their own queue, giving independent scaling + resource limits per task type.
#
#  Queue name     Worker container    Purpose
#  ─────────────  ──────────────────  ────────────────────────────────────────
#  ingest_tasks   worker-ingest       PDF parse, chunk accumulation, orchestration
#  vision_tasks   worker-vision       Chart/image descriptions (OpenAI → Anthropic)
#  math_tasks     worker-math         Math formula extraction (OpenAI → Anthropic)
#  clean_tasks    worker-clean        PDF noise removal, text normalisation
#  embed_tasks    worker-embed        Embedding generation (Gemini → OpenAI)
#  gen_tasks      worker-gen          Question generation (OpenAI → Anthropic → Gemini)
#  mark_tasks     worker-mark         Answer marking (OpenAI → Anthropic → Gemini)

_default_exchange = Exchange("default", type="direct")

TASK_QUEUES = (
    Queue("ingest_tasks", _default_exchange, routing_key="ingest_tasks"),
    Queue("vision_tasks",  _default_exchange, routing_key="vision_tasks"),
    Queue("math_tasks",    _default_exchange, routing_key="math_tasks"),
    Queue("clean_tasks",   _default_exchange, routing_key="clean_tasks"),
    Queue("embed_tasks",   _default_exchange, routing_key="embed_tasks"),
    Queue("deepsearch_tasks", _default_exchange, routing_key="deepsearch_tasks"),
    Queue("gen_tasks",     _default_exchange, routing_key="gen_tasks"),
    Queue("mark_tasks",    _default_exchange, routing_key="mark_tasks"),
)

# ── Task routing: task name → queue ───────────────────────────────────────────
TASK_ROUTES = {
    # Ingestion
    "app.tasks.ingest_tasks.ingest_book_resumable_task": {"queue": "ingest_tasks"},
    "app.tasks.ingest_tasks.ingest_book_only_task":      {"queue": "ingest_tasks"},
    "app.tasks.ingest_tasks.ingest_pdf_task":            {"queue": "ingest_tasks"},
    "app.tasks.ingest_tasks.generate_from_book_task":    {"queue": "gen_tasks"},
    # Cleaning
    "app.tasks.clean_tasks.clean_book_chunks_task":      {"queue": "clean_tasks"},
    "app.tasks.clean_tasks.clean_all_chunks_task":       {"queue": "clean_tasks"},
    "app.tasks.clean_tasks.clean_chunk_by_id_task":      {"queue": "clean_tasks"},
    # DeepSearch (RAG retrieval for question generation)
    "app.tasks.deepsearch_tasks.*":                      {"queue": "deepsearch_tasks"},
    # Marking
    "app.tasks.marking_tasks.*":                         {"queue": "mark_tasks"},
}

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,

    # ── Queue config ────────────────────────────────────────────────────────────
    task_queues=TASK_QUEUES,
    task_routes=TASK_ROUTES,
    task_default_queue="ingest_tasks",

    # ── Production hardening ────────────────────────────────────────────────────
    task_soft_time_limit=1800,
    task_time_limit=2100,
    task_acks_late=True,
    worker_max_tasks_per_child=50,
    worker_prefetch_multiplier=1,
    result_expires=3600,
)

celery_app.autodiscover_tasks(["app.tasks"])
