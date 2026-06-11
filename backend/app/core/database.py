import asyncio

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from app.core.config import settings

_client: AsyncIOMotorClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def get_client() -> AsyncIOMotorClient:
    """
    Return a Motor client bound to the *current* event loop.

    Celery tasks create a fresh event loop per execution. A client cached from
    a previous task stays bound to that task's (now closed) loop and raises
    "RuntimeError: Event loop is closed" on first use — so rebind whenever the
    running loop changes. FastAPI always runs on one loop, so the client is
    created once and reused there.
    """
    global _client, _client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _client is None or _client_loop is not loop:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = AsyncIOMotorClient(
            settings.MONGODB_URL,
            serverSelectionTimeoutMS=5000,
        )
        _client_loop = loop
    return _client


def get_mongo_db() -> AsyncIOMotorDatabase:
    return get_client()[settings.MONGODB_DB_NAME]


async def get_db() -> AsyncIOMotorDatabase:
    """FastAPI dependency — returns the MongoDB database."""
    return get_mongo_db()
