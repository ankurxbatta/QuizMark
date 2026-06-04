"""
admin.py — Admin endpoints for API key usage monitoring and rotation status.
Providers: Gemini (embeddings) · OpenAI (primary) · Anthropic (fallback)
"""
from __future__ import annotations

import asyncio
import httpx
from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.services.api_key_manager import key_manager
from app.core.config import settings

router = APIRouter()


async def _probe_gemini_embed() -> dict:
    if not settings.GEMINI_API_KEY:
        return {"provider": "gemini_embed", "reachable": False, "note": "key not set"}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{settings.GEMINI_BASE_URL}/models/{settings.GEMINI_EMBEDDING_MODEL}:embedContent",
                params={"key": settings.GEMINI_API_KEY},
                json={"model": f"models/{settings.GEMINI_EMBEDDING_MODEL}",
                      "content": {"parts": [{"text": "test"}]},
                      "taskType": "SEMANTIC_SIMILARITY", "outputDimensionality": 768},
            )
        if r.status_code == 200:
            return {"provider": "gemini_embed", "reachable": True, "status_code": 200}
        return {"provider": "gemini_embed", "reachable": False,
                "status_code": r.status_code, "error": r.text[:300]}
    except Exception as exc:
        return {"provider": "gemini_embed", "reachable": False, "error": str(exc)[:200]}


async def _probe_openai_embed() -> dict:
    if not settings.OPENAI_API_KEY:
        return {"provider": "openai_embed", "reachable": False, "note": "key not set"}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{settings.OPENAI_BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={"model": settings.OPENAI_EMBEDDING_MODEL, "input": "test", "dimensions": 768},
            )
        if r.status_code == 200:
            return {"provider": "openai_embed", "reachable": True, "status_code": 200}
        return {"provider": "openai_embed", "reachable": False,
                "status_code": r.status_code, "error": r.text[:300]}
    except Exception as exc:
        return {"provider": "openai_embed", "reachable": False, "error": str(exc)[:200]}


async def _probe_openai_vision() -> dict:
    if not settings.OPENAI_API_KEY:
        return {"provider": "openai_vision", "reachable": False, "note": "key not set"}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{settings.OPENAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={"model": settings.OPENAI_GENERATION_MODEL,
                      "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5},
            )
        if r.status_code == 200:
            return {"provider": "openai_generation", "reachable": True, "status_code": 200,
                    "model": settings.OPENAI_GENERATION_MODEL}
        return {"provider": "openai_generation", "reachable": False,
                "status_code": r.status_code, "error": r.text[:300]}
    except Exception as exc:
        return {"provider": "openai_generation", "reachable": False, "error": str(exc)[:200]}


async def _probe_anthropic() -> dict:
    if not settings.ANTHROPIC_API_KEY:
        return {"provider": "anthropic", "reachable": False, "note": "key not set"}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": settings.ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": settings.ANTHROPIC_GENERATION_MODEL,
                      "max_tokens": 10, "messages": [{"role": "user", "content": "Hi"}]},
            )
        if r.status_code == 200:
            return {"provider": "anthropic", "reachable": True, "status_code": 200,
                    "model": settings.ANTHROPIC_GENERATION_MODEL}
        return {"provider": "anthropic", "reachable": False,
                "status_code": r.status_code, "error": r.text[:300]}
    except Exception as exc:
        return {"provider": "anthropic", "reachable": False, "error": str(exc)[:200]}


@router.get("/api-status")
async def api_status(current_user: dict = Depends(get_current_user)):
    """Live probe of all configured API providers + rotation stats."""
    probes = await asyncio.gather(
        _probe_gemini_embed(), _probe_openai_embed(),
        _probe_openai_vision(), _probe_anthropic(),
        return_exceptions=True,
    )
    results = [p if not isinstance(p, Exception) else {"error": str(p)} for p in probes]

    return {
        "live_probes": results,
        "rotation_stats": key_manager.stats(),
        "config": {
            "gemini_key_set": bool(settings.GEMINI_API_KEY),
            "openai_key_set": bool(settings.OPENAI_API_KEY),
            "anthropic_key_set": bool(settings.ANTHROPIC_API_KEY),
            "embedding_chain": ["gemini_embed (free, 768-dim)", "openai_embed (paid, 768-dim)"],
            "vision_chain": ["openai_vision (gpt-4o-mini)", "anthropic_vision (claude-haiku)"],
            "generation_chain": ["openai (gpt-4o-mini)", "anthropic (claude-haiku)", "gemini (fallback)"],
            "marking_chain": ["openai (gpt-4o-mini)", "anthropic (claude-haiku)", "gemini (fallback)"],
            "active_generation_provider": settings.GENERATION_LLM_PROVIDER,
            "active_marking_provider": settings.ONLINE_LLM_PROVIDER,
        },
    }


@router.post("/clean/book/{book_id}")
async def trigger_clean_book(book_id: str, current_user: dict = Depends(get_current_user)):
    """Trigger async cleaning of all chunks for a specific book."""
    from app.tasks.clean_tasks import clean_book_chunks_task
    task = clean_book_chunks_task.delay(book_id)
    return {"task_id": task.id, "book_id": book_id, "status": "queued", "queue": "clean_tasks"}


@router.post("/clean/all")
async def trigger_clean_all(current_user: dict = Depends(get_current_user)):
    """Trigger async cleaning of ALL chunks in the database."""
    from app.tasks.clean_tasks import clean_all_chunks_task
    task = clean_all_chunks_task.delay()
    return {"task_id": task.id, "status": "queued", "queue": "clean_tasks"}


@router.get("/clean/preview/{book_id}")
async def preview_noise(book_id: str, current_user: dict = Depends(get_current_user)):
    """Show sample noisy chunks before running the cleaner."""
    from app.core.database import get_mongo_db
    from app.services.text_cleaner import estimate_noise_ratio, clean_text
    db = get_mongo_db()
    chunks = await db["pdf_chunks"].find(
        {"book_id": book_id}, {"text": 1, "chapter_title": 1, "page_start": 1}
    ).limit(200).to_list(length=200)
    noisy = []
    for c in chunks:
        text = c.get("text", "")
        noise = estimate_noise_ratio(text)
        if noise > 0.005:
            noisy.append({
                "id": str(c["_id"]), "page": c.get("page_start"),
                "chapter": c.get("chapter_title"), "noise_ratio": round(noise, 4),
                "original_snippet": text[:300], "cleaned_snippet": clean_text(text)[:300],
            })
    noisy.sort(key=lambda x: x["noise_ratio"], reverse=True)
    return {"book_id": book_id, "sampled": len(chunks),
            "noisy_count": len(noisy), "top_noisy": noisy[:10]}


@router.post("/api-status/reset-cooldowns")
async def reset_cooldowns(current_user: dict = Depends(get_current_user)):
    """Reset all provider cooldowns (use after adding new API keys)."""
    for health in key_manager._health.values():
        health.rate_limited_until = 0.0
        health.quota_exhausted_until = 0.0
    return {"message": "All cooldowns reset", "providers": list(key_manager._health.keys())}
