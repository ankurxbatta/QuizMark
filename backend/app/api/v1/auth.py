import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.database import get_db
from app.core.security import (
    verify_password,
    hash_password,
    create_access_token,
    get_current_user,
    require_instructor,
    login_limiter,
    login_ip_limiter,
    register_limiter,
    get_client_ip,
)
from app.core.config import settings
from app.schemas.schemas import LoginRequest, TokenResponse, UserOut

router = APIRouter()


class RegisterRequest(BaseModel):
    username: str
    password: str


def _user_out(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "username": doc["username"],
        "role": doc["role"],
        "created_at": doc["created_at"],
    }


@router.post("/register", response_model=UserOut, status_code=201)
async def register(payload: RegisterRequest, request: Request, db: AsyncIOMotorDatabase = Depends(get_db)):
    ip = get_client_ip(request)
    register_limiter.check(ip)
    username = payload.username.strip()
    if not username:
        raise HTTPException(400, "Username is required")
    if len(payload.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    if await db["users"].find_one({"username": username}):
        raise HTTPException(409, "Username already taken")

    user_doc = {
        "_id": str(uuid.uuid4()),
        "username": username,
        "hashed_password": hash_password(payload.password),
        "role": "student",
        "failed_attempts": 0,
        "locked_until": None,
        "created_at": datetime.now(timezone.utc),
    }
    await db["users"].insert_one(user_doc)
    return _user_out(user_doc)


@router.get("/students", response_model=list[UserOut])
async def list_students(
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    cursor = db["users"].find({"role": "student"}).sort("username", 1)
    docs = await cursor.to_list(length=1000)
    return [_user_out(d) for d in docs]


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, db: AsyncIOMotorDatabase = Depends(get_db)):
    ip = get_client_ip(request)
    # Per-IP ceiling first (classroom-NAT safe), then the per-account
    # brute-force limit — see the limiter definitions in core/security.py.
    login_ip_limiter.check(ip)
    login_limiter.check(f"{ip}:{payload.username.strip().lower()}")
    user = await db["users"].find_one({"username": payload.username})
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    now = datetime.now(timezone.utc)
    locked_until = user.get("locked_until")
    if locked_until is not None:
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > now:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Account locked. Try again after {locked_until.isoformat()}",
            )

    if not verify_password(payload.password, user["hashed_password"]):
        attempts = user.get("failed_attempts", 0) + 1
        update = {"failed_attempts": attempts}
        if attempts >= settings.MAX_FAILED_LOGIN_ATTEMPTS:
            update["locked_until"] = now + timedelta(minutes=settings.LOCKOUT_DURATION_MINUTES)
            update["failed_attempts"] = 0
        await db["users"].update_one({"_id": user["_id"]}, {"$set": update})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    await db["users"].update_one(
        {"_id": user["_id"]},
        {"$set": {"failed_attempts": 0, "locked_until": None}},
    )
    token = create_access_token(
        {"sub": user["_id"], "role": user["role"], "auth_time": int(now.timestamp())}
    )
    return TokenResponse(access_token=token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    claims: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Exchange a still-valid token for a fresh one (sliding session).

    The original login time travels in the ``auth_time`` claim; once the
    session is older than SESSION_MAX_MINUTES the client must log in again,
    so a leaked token cannot be renewed indefinitely.
    """
    now = datetime.now(timezone.utc)
    auth_time = claims.get("auth_time") or int(now.timestamp())
    if now.timestamp() - auth_time > settings.SESSION_MAX_MINUTES * 60:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please log in again.",
        )

    user = await db["users"].find_one({"_id": claims["sub"]})
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    locked_until = user.get("locked_until")
    if locked_until is not None:
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > now:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account locked")

    token = create_access_token(
        {"sub": user["_id"], "role": user["role"], "auth_time": auth_time}
    )
    return TokenResponse(access_token=token)
