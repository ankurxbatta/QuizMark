from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1 import auth, questions, submissions, marking, export
from app.core.config import settings

app = FastAPI(
    title="Quiz Generation & Marking API",
    version="1.0.0",
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

app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(questions.router, prefix="/api/v1/questions", tags=["questions"])
app.include_router(submissions.router, prefix="/api/v1/submissions", tags=["submissions"])
app.include_router(marking.router, prefix="/api/v1/marking", tags=["marking"])
app.include_router(export.router, prefix="/api/v1/export", tags=["export"])


@app.get("/health")
async def health_check():
    return {"status": "ok"}
