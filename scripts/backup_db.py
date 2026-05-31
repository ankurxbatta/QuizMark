import asyncio
import json
from motor.motor_asyncio import AsyncIOMotorClient
from bson import json_util
import os

DB_NAME = "marking_tools"
COLLECTIONS = ["pdf_chunks", "questions", "ingest_jobs"]
BACKUP_DIR = "database_backup"

async def backup():
    client = AsyncIOMotorClient("mongodb://localhost:27017/?directConnection=true")
    db = client[DB_NAME]
    
    os.makedirs(BACKUP_DIR, exist_ok=True)
    
    for coll_name in COLLECTIONS:
        print(f"Backing up '{coll_name}'...")
        docs = await db[coll_name].find({}).to_list(length=None)
        
        file_path = os.path.join(BACKUP_DIR, f"{coll_name}.json")
        with open(file_path, "w") as f:
            f.write(json_util.dumps(docs, indent=2))
        
        print(f" -> Saved {len(docs)} records to {file_path}")

    print("Backup complete!")

if __name__ == "__main__":
    asyncio.run(backup())
