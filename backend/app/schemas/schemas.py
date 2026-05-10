from pydantic import BaseModel
from typing import Optional
from uuid import UUID
from datetime import datetime
from app.models.models import QuestionType, Difficulty


class QuestionCreate(BaseModel):
    question_text: str
    question_type: QuestionType
    model_answer: str
    rubric: str
    max_marks: float
    topic_tag: Optional[str] = None
    difficulty: Optional[Difficulty] = None


class QuestionUpdate(QuestionCreate):
    pass


class QuestionOut(QuestionCreate):
    id: UUID
    source_page_range: Optional[str] = None
    source_chunk: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class SubmissionCreate(BaseModel):
    question_id: UUID
    answer_text: str


class SubmissionOut(BaseModel):
    id: UUID
    student_id: UUID
    question_id: UUID
    answer_text: str
    auto_mark: Optional[float] = None
    auto_feedback: Optional[str] = None
    auto_confidence: Optional[float] = None
    marking_route: Optional[str] = None
    override_mark: Optional[float] = None
    override_feedback: Optional[str] = None
    is_flagged: bool
    is_marked: bool
    submitted_at: datetime

    class Config:
        from_attributes = True


class OverrideRequest(BaseModel):
    override_mark: float
    override_feedback: str
    override_reason: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    username: str
    password: str
