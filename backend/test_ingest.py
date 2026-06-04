import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import uuid
from datetime import datetime, timezone

async def main():
    with open('../Book/IntroductoryBusinessStatistics-OP.pdf', 'rb') as f:
        raw_bytes = f.read()
    
    filename = 'IntroductoryBusinessStatistics-OP.pdf'
    book_id = filename.rsplit('.', 1)[0]
    job_id = str(uuid.uuid4())
    
    client = AsyncIOMotorClient('mongodb://mongodb:27017/')
    db = client.marking_tools
    
    job_doc = {
        "_id": job_id,
        "filename": filename,
        "book_id": book_id,
        "total_pages": 0,
        "question_type": "none",
        "count_per_chapter": 0,
        "status": "queued",
        "chapters_done": 0,
        "questions_created": 0,
        "total_chapters": 0,
        "current_chapter": None,
        "current_chapter_title": None,
        "progress_message": "Queued for library ingestion...",
        "last_heartbeat_at": datetime.now(timezone.utc),
        "error_message": None,
        "started_at": None,
        "completed_at": None,
        "created_at": datetime.now(timezone.utc),
    }
    await db.ingest_jobs.insert_one(job_doc)
    
    from app.tasks.ingest_tasks import ingest_book_only_task
    ingest_book_only_task.delay(raw_bytes, filename, job_id, book_id)
    print(f"Started job {job_id}")

if __name__ == '__main__':
    asyncio.run(main())
