import asyncio
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1 import auth, questions, submissions, marking, export, analytics
from app.api.v1 import admin as admin_v1
from app.core.config import settings
from app.core.database import get_mongo_db
from app.core.security import hash_password, verify_password

logger = logging.getLogger("app.startup")

_is_production = settings.ENVIRONMENT == "production"

app = FastAPI(
    title="Quiz Generation & Marking API",
    version="2.0.0",
    docs_url=None if _is_production else "/docs",
    redoc_url=None if _is_production else "/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(auth.router,        prefix="/api/v1/auth",        tags=["auth"])
app.include_router(questions.router,   prefix="/api/v1/questions",   tags=["questions"])
app.include_router(submissions.router, prefix="/api/v1/submissions",  tags=["submissions"])
app.include_router(marking.router,     prefix="/api/v1/marking",     tags=["marking"])
app.include_router(export.router,      prefix="/api/v1/export",      tags=["export"])
app.include_router(analytics.router,   prefix="/api/v1/analytics",   tags=["analytics"])
app.include_router(admin_v1.router,    prefix="/api/v1/admin",        tags=["admin"])


@app.get("/health")
async def health_check():
    return {"status": "ok"}


async def _wait_for_mongo(max_attempts: int = 15, delay: float = 3.0):
    """Retry MongoDB connection until the replica-set primary is elected."""
    for attempt in range(max_attempts):
        try:
            db = get_mongo_db()
            await db.command("ping")
            return db
        except Exception as exc:
            if attempt < max_attempts - 1:
                logger.warning(f"[STARTUP] MongoDB not ready (attempt {attempt + 1}/{max_attempts}): {exc!r} — retrying in {delay}s")
                await asyncio.sleep(delay)
            else:
                raise


@app.on_event("startup")
async def ensure_admin_user():
    if not settings.ADMIN_ENABLED or not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD:
        return
    try:
        import uuid
        from datetime import datetime, timezone
        db = await _wait_for_mongo()
        user = await db["users"].find_one({"username": settings.ADMIN_USERNAME})
        desired_role = settings.ADMIN_ROLE
        if not user:
            await db["users"].insert_one({
                "_id": str(uuid.uuid4()),
                "username": settings.ADMIN_USERNAME,
                "hashed_password": hash_password(settings.ADMIN_PASSWORD),
                "role": desired_role,
                "failed_attempts": 0,
                "locked_until": None,
                "created_at": datetime.now(timezone.utc),
            })
            logger.info(f"[STARTUP] Admin user '{settings.ADMIN_USERNAME}' created.")
            return
        needs_update = (
            user["role"] != desired_role
            or not verify_password(settings.ADMIN_PASSWORD, user["hashed_password"])
        )
        if needs_update:
            await db["users"].update_one(
                {"_id": user["_id"]},
                {"$set": {"role": desired_role, "hashed_password": hash_password(settings.ADMIN_PASSWORD)}},
            )
    except Exception as exc:
        logger.warning(f"[STARTUP] Admin user setup failed (non-fatal): {exc}")


@app.on_event("startup")
async def ensure_mongo_indexes():
    try:
        db = await _wait_for_mongo()

        from app.services.mongo_vector_store import ensure_vector_index
        try:
            await ensure_vector_index()
        except Exception as exc:
            logger.warning(f"[MONGO] Vector index setup failed (non-fatal): {exc}")

        await db["users"].create_index("username", unique=True)
        await db["questions"].create_index("topic_tag")
        await db["questions"].create_index("created_at")
        await db["submissions"].create_index("student_id")
        await db["submissions"].create_index("question_id")
        await db["submissions"].create_index("is_flagged")
        await db["submissions"].create_index("is_marked")
        await db["submissions"].create_index([("is_marked", 1), ("auto_confidence", 1)])
        await db["audit_logs"].create_index("timestamp")
        await db["ingest_jobs"].create_index("created_at")
        await db["pdf_chunks"].create_index("book_hash")
        await db["pdf_chunks"].create_index("book_id")
        await db["ingest_checkpoints"].create_index("updated_at")
        await db["ingest_checkpoints"].create_index("status")
        logger.info("[STARTUP] MongoDB indexes ready.")
    except Exception as exc:
        logger.warning(f"[STARTUP] MongoDB index setup failed (non-fatal): {exc}")


@app.on_event("startup")
async def backfill_specialist_indexes():
    """
    Auto-backfill (MULTI_RAG_DESIGN): enqueue specialist index builds for books
    that are fully ingested but have no build recorded. Guarded by a per-book
    marker in index_build_jobs so restarts never re-enqueue.
    """
    if not settings.MATH_INDEX_ENABLED:
        return
    try:
        from datetime import datetime, timezone

        db = get_mongo_db()
        book_ids = await db["pdf_chunks"].distinct("book_id")
        if not book_ids:
            return
        from app.tasks.index_tasks import build_math_index_task

        enqueued = 0
        for book_id in book_ids:
            if not book_id:
                continue
            marker = await db["index_build_jobs"].find_one({"_id": f"math:{book_id}"})
            if marker:
                continue
            await db["index_build_jobs"].insert_one({
                "_id": f"math:{book_id}", "index": "math", "book_id": book_id,
                "status": "queued", "started_at": None, "finished_at": None,
                "created_at": datetime.now(timezone.utc),
            })
            build_math_index_task.delay(book_id)
            enqueued += 1
        if enqueued:
            logger.info(f"[STARTUP] Backfill: enqueued math index builds for {enqueued} book(s).")
    except Exception as exc:
        logger.warning(f"[STARTUP] Specialist index backfill failed (non-fatal): {exc}")
