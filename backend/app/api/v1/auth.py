from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta, timezone
from app.core.database import get_db
from app.core.security import verify_password, hash_password, create_access_token, require_instructor
from app.models.models import User, UserRole
from app.schemas.schemas import LoginRequest, TokenResponse, UserOut
from app.core.config import settings
from pydantic import BaseModel

router = APIRouter()


class RegisterRequest(BaseModel):
    username: str
    password: str


@router.post("/register", response_model=UserOut, status_code=201)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Create a new student account."""
    username = payload.username.strip()
    if not username:
        raise HTTPException(400, "Username is required")
    if len(payload.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    result = await db.execute(select(User).where(User.username == username))
    if result.scalar_one_or_none():
        raise HTTPException(409, "Username already taken")

    user = User(
        username=username,
        hashed_password=hash_password(payload.password),
        role=UserRole.student,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/students", response_model=list[UserOut])
async def list_students(db: AsyncSession = Depends(get_db), _: dict = Depends(require_instructor)):
    result = await db.execute(
        select(User)
        .where(User.role == UserRole.student)
        .order_by(User.username.asc())
    )
    return result.scalars().all()


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # Check lockout
    now = datetime.now(timezone.utc)
    locked_until = user.locked_until
    if locked_until is not None:
        # Handle both naive and timezone-aware datetimes in DB
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > now:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Account locked. Try again after {locked_until.isoformat()}",
            )

    if not verify_password(payload.password, user.hashed_password):
        user.failed_attempts += 1
        if user.failed_attempts >= settings.MAX_FAILED_LOGIN_ATTEMPTS:
            user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=settings.LOCKOUT_DURATION_MINUTES)
            user.failed_attempts = 0
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # Reset on success
    user.failed_attempts = 0
    user.locked_until = None
    await db.commit()

    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    return TokenResponse(access_token=token)
