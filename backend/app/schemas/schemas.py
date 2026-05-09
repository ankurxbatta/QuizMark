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
    auto_mark: Optional[float]
    auto_feedback: Optional[str]
    override_mark: Optional[float]
    override_feedback: Optional[str]
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
