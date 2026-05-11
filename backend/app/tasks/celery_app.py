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
)

# Ensure task modules are registered with the worker.
celery_app.autodiscover_tasks(["app.tasks"])
