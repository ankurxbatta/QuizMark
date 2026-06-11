import asyncio
import sys
import uuid
from datetime import datetime, timezone

from app.core.config import settings
from app.core.database import get_mongo_db
from app.core.security import hash_password


DEMO_USERS = {
    "student1": ("student123", "student"),
    "student2": ("student123", "student"),
    "student3": ("student123", "student"),
}


async def upsert_user(db, username: str, password: str, role: str) -> dict:
    existing = await db["users"].find_one({"username": username})
    if existing:
        await db["users"].update_one(
            {"_id": existing["_id"]},
            {"$set": {
                "hashed_password": hash_password(password),
                "role": role,
                "failed_attempts": 0,
                "locked_until": None,
            }},
        )
        return {**existing, "role": role}
    doc = {
        "_id": str(uuid.uuid4()),
        "username": username,
        "hashed_password": hash_password(password),
        "role": role,
        "failed_attempts": 0,
        "locked_until": None,
        "created_at": datetime.now(timezone.utc),
    }
    await db["users"].insert_one(doc)
    return doc


async def main():
    if settings.ENVIRONMENT != "development":
        print(
            "Refusing to seed demo data: ENVIRONMENT is "
            f"'{settings.ENVIRONMENT}', not 'development'. "
            "Demo users have hardcoded credentials and must never be created outside development."
        )
        sys.exit(1)
    db = get_mongo_db()
    for username, (password, role) in DEMO_USERS.items():
        user = await upsert_user(db, username, password, role)
        print(f"Seeded: {username} / {password} (role={user['role']})")


if __name__ == "__main__":
    asyncio.run(main())
