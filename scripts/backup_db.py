"""
backup_db.py — JSON dump of the MongoDB database (all collections).

Usage (from project root):
    backend/.venv-test/bin/python scripts/backup_db.py

Configuration (environment variables):
    MONGODB_URL      — connection string (default mongodb://localhost:27017/?directConnection=true)
    MONGODB_DB_NAME  — database name    (default marking_tools)
    BACKUP_DIR       — output directory (default database_backup)

NOTE: the in-repo .env uses the docker-internal hostname (mongodb://mongodb:...),
so it is intentionally NOT auto-loaded here — set MONGODB_URL explicitly when
backing up a non-local server.

Every collection in the database is exported (including GridFS buckets such as
book_pdfs.files / book_pdfs.chunks — binary data is preserved via BSON extended
JSON). For large production databases prefer `mongodump` (see backup-database/),
which is faster and streams; this script is the dependency-light fallback.
"""
import asyncio
import os

from motor.motor_asyncio import AsyncIOMotorClient
from bson import json_util

MONGODB_URL = os.environ.get(
    "MONGODB_URL", "mongodb://localhost:27017/?directConnection=true"
)
DB_NAME = os.environ.get("MONGODB_DB_NAME", "marking_tools")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "database_backup")


async def backup() -> None:
    client = AsyncIOMotorClient(MONGODB_URL, serverSelectionTimeoutMS=10000)
    db = client[DB_NAME]

    names = sorted(await db.list_collection_names())
    if not names:
        print(f"No collections found in database '{DB_NAME}' at {MONGODB_URL}")
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    print(f"Backing up '{DB_NAME}' ({len(names)} collections) -> {BACKUP_DIR}/")

    for coll_name in names:
        docs = await db[coll_name].find({}).to_list(length=None)
        file_path = os.path.join(BACKUP_DIR, f"{coll_name}.json")
        with open(file_path, "w") as f:
            f.write(json_util.dumps(docs, indent=2))
        print(f" -> {coll_name}: {len(docs)} documents saved to {file_path}")

    print("Backup complete!")


if __name__ == "__main__":
    asyncio.run(backup())
