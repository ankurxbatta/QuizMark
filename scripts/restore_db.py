"""
restore_db.py — restore a JSON dump created by scripts/backup_db.py.

Usage (from project root):
    backend/.venv-test/bin/python scripts/restore_db.py

Configuration (environment variables):
    MONGODB_URL      — connection string (default mongodb://localhost:27017/?directConnection=true)
    MONGODB_DB_NAME  — database name    (default marking_tools)
    BACKUP_DIR       — input directory  (default database_backup)

NOTE: the in-repo .env uses the docker-internal hostname (mongodb://mongodb:...),
so it is intentionally NOT auto-loaded here — set MONGODB_URL explicitly when
restoring to a non-local server.

Restores every <collection>.json file found in BACKUP_DIR. WARNING: each
restored collection is dropped first, so existing data in those collections is
replaced by the backup's contents.
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


async def restore() -> None:
    if not os.path.isdir(BACKUP_DIR):
        print(f"Error: backup directory '{BACKUP_DIR}' not found.")
        return

    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".json"))
    if not files:
        print(f"Error: no .json backup files found in '{BACKUP_DIR}'.")
        return

    client = AsyncIOMotorClient(MONGODB_URL, serverSelectionTimeoutMS=10000)
    db = client[DB_NAME]
    print(f"Restoring {len(files)} collections into '{DB_NAME}' at {MONGODB_URL}")

    for filename in files:
        coll_name = filename[: -len(".json")]
        file_path = os.path.join(BACKUP_DIR, filename)
        with open(file_path, "r") as f:
            raw_data = f.read()
        if not raw_data.strip():
            print(f"Skipping '{coll_name}' (empty file).")
            continue
        docs = json_util.loads(raw_data)
        if not docs:
            print(f"Skipping '{coll_name}' (no documents).")
            continue

        # Drop existing collection to ensure a clean restore.
        await db[coll_name].drop()
        await db[coll_name].insert_many(docs)
        print(f" -> {coll_name}: {len(docs)} documents restored")

    print(
        "Restore complete. NOTE: plain B-tree indexes are recreated on backend "
        "startup, and Atlas vector search indexes are re-ensured on first write."
    )


if __name__ == "__main__":
    asyncio.run(restore())
