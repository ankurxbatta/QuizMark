import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from bson import json_util
import os

DB_NAME = "marking_tools"
COLLECTIONS = ["pdf_chunks", "questions", "ingest_jobs"]
BACKUP_DIR = "database_backup"

async def restore():
    client = AsyncIOMotorClient("mongodb://localhost:27017/?directConnection=true")
    db = client[DB_NAME]
    
    if not os.path.exists(BACKUP_DIR):
        print(f"Error: {BACKUP_DIR} directory not found.")
        return

    for coll_name in COLLECTIONS:
        file_path = os.path.join(BACKUP_DIR, f"{coll_name}.json")
        if not os.path.exists(file_path):
            print(f"Skipping '{coll_name}', file not found.")
            continue
            
        print(f"Restoring '{coll_name}'...")
        with open(file_path, "r") as f:
            raw_data = f.read()
            if not raw_data.strip():
                continue
            docs = json_util.loads(raw_data)
        
        if docs:
            # Drop existing collection to ensure clean restore
            await db[coll_name].drop()
            # Insert all documents
            await db[coll_name].insert_many(docs)
            print(f" -> Restored {len(docs)} records to {coll_name}")

    print("Restore complete! Database is fully populated.")

if __name__ == "__main__":
    asyncio.run(restore())
