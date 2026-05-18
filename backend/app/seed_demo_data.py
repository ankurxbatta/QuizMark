import asyncio

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.security import hash_password
from app.models.models import User, UserRole


DEMO_PASSWORDS = {
    "student1": "student123",
    "student2": "student123",
    "student3": "student123",
}


async def upsert_user(username: str, password: str, role: UserRole) -> User:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(username=username, hashed_password=hash_password(password), role=role)
            session.add(user)
        else:
            user.hashed_password = hash_password(password)
            user.role = role
            user.failed_attempts = 0
            user.locked_until = None
        await session.commit()
        await session.refresh(user)
        return user


async def main():
    students = [
        await upsert_user("student1", DEMO_PASSWORDS["student1"], UserRole.student),
        await upsert_user("student2", DEMO_PASSWORDS["student2"], UserRole.student),
        await upsert_user("student3", DEMO_PASSWORDS["student3"], UserRole.student),
    ]

    print("Seeded demo student accounts.")
    for student in students:
        print(f"Student: {student.username} / {DEMO_PASSWORDS[student.username]}")


if __name__ == "__main__":
    asyncio.run(main())
