import enum


class QuestionType(str, enum.Enum):
    mcq = "mcq"
    true_false = "true_false"
    short_answer = "short_answer"


class Difficulty(str, enum.Enum):
    easy = "easy"
    medium = "medium"
    hard = "hard"


class UserRole(str, enum.Enum):
    instructor = "instructor"
    student = "student"


class MarkingRoute(str, enum.Enum):
    HIGH = "HIGH"
    MID = "MID"
    LOW = "LOW"


class IngestJobStatus(str, enum.Enum):
    queued     = "queued"
    processing = "processing"
    done       = "done"
    failed     = "failed"


# ── MongoDB document shapes (for reference / type hints) ──────────────────────
#
# users:               {_id, username, hashed_password, role, failed_attempts,
#                       locked_until, created_at}
#
# questions:           {_id, question_text, question_type, model_answer, rubric,
#                       max_marks, topic_tag, difficulty, source_page_range,
#                       source_chunk, embedding, assigned_student_ids, created_at}
#
# submissions:         {_id, student_id, question_id, answer_text,
#                       auto_mark, auto_feedback, auto_confidence, marking_route,
#                       slm_keyword_coverage, slm_semantic_sim, slm_raw_score,
#                       override_mark, override_feedback, override_reason,
#                       is_flagged, is_marked, submitted_at, marked_at}
#
# ingest_jobs:         {_id, filename, total_pages, question_type,
#                       count_per_chapter, status, chapters_done,
#                       questions_created, total_chapters, current_chapter,
#                       current_chapter_title, progress_message,
#                       last_heartbeat_at, error_message, started_at,
#                       completed_at, created_at}
#
# audit_logs:          {_id, event_type, actor_id, submission_id, detail,
#                       timestamp}
#
# pdf_chunks:          {_id, book_id, chapter_num, chapter_title,
#                       section_title, topic_tag, text, image_texts,
#                       table_texts, math_text, page_start, page_end,
#                       has_images, has_tables, has_math, has_formula,
#                       has_example, teaching_density, key_terms,
#                       embedding, created_at}
#
# page_description_cache: {_id (md5), description, created_at}
