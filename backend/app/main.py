from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from app.api.v1 import auth, questions, submissions, marking, export, analytics
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.security import hash_password, verify_password
from app.models.models import User, UserRole

app = FastAPI(
    title="Quiz Generation & Marking API",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,        prefix="/api/v1/auth",        tags=["auth"])
app.include_router(questions.router,   prefix="/api/v1/questions",   tags=["questions"])
app.include_router(submissions.router, prefix="/api/v1/submissions",  tags=["submissions"])
app.include_router(marking.router,     prefix="/api/v1/marking",     tags=["marking"])
app.include_router(export.router,      prefix="/api/v1/export",      tags=["export"])
app.include_router(analytics.router,   prefix="/api/v1/analytics",   tags=["analytics"])


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "2.0.0"}


@app.on_event("startup")
async def ensure_admin_user():
    if not settings.ADMIN_ENABLED:
        return
    if not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD:
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.username == settings.ADMIN_USERNAME)
        )
        user = result.scalar_one_or_none()

        desired_role = UserRole(settings.ADMIN_ROLE)
        desired_hash = hash_password(settings.ADMIN_PASSWORD)

        if not user:
            user = User(
                username=settings.ADMIN_USERNAME,
                hashed_password=desired_hash,
                role=desired_role,
            )
            session.add(user)
            await session.commit()
            return

        # Keep role and password in sync with config.
        needs_update = user.role != desired_role or not verify_password(
            settings.ADMIN_PASSWORD, user.hashed_password
        )
        if needs_update:
            user.role = desired_role
            user.hashed_password = desired_hash
            await session.commit()
