from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "quiz_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.marking_tasks",
        "app.tasks.ingest_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # ── Production hardening ──────────────────────────────────────────────────
    task_soft_time_limit=1800,       # 30 min soft limit → SoftTimeLimitExceeded
    task_time_limit=2100,            # 35 min hard kill (safety net)
    task_acks_late=True,             # Ack after completion, not on receive
    worker_max_tasks_per_child=50,   # Recycle worker after 50 tasks (prevent memory leaks)
    worker_prefetch_multiplier=1,    # Don't prefetch tasks (large PDF payloads in args)
    result_expires=3600,             # Expire results after 1 hour
)

# Ensure task modules are registered with the worker.
celery_app.autodiscover_tasks(["app.tasks"])
