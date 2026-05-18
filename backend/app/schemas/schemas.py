from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
from uuid import UUID
from datetime import datetime
from app.models.models import QuestionType, Difficulty


class QuestionCreate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

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
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: UUID
    source_page_range: Optional[str] = None
    source_chunk: Optional[str] = None
    assigned_student_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime


class AssessmentQuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    question_text: str
    question_type: QuestionType
    max_marks: float
    topic_tag: Optional[str] = None
    difficulty: Optional[Difficulty] = None
    source_page_range: Optional[str] = None
    source_chunk: Optional[str] = None
    created_at: datetime


class QuestionAssigneeUpdate(BaseModel):
    student_ids: list[UUID] = Field(default_factory=list)


class QuestionAssigneeOut(BaseModel):
    question_id: UUID
    student_ids: list[UUID] = Field(default_factory=list)


class QuestionGenerateResponse(BaseModel):
    generated: int
    source_file: str
    source_pages: Optional[int] = None
    chunks_processed: Optional[int] = None
    topics_covered: Optional[list[str]] = None
    questions: list[QuestionOut]


class SubmissionCreate(BaseModel):
    question_id: UUID
    answer_text: str


class SubmissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    student_id: UUID
    question_id: UUID
    question_text: Optional[str] = None
    question_type: Optional[QuestionType] = None
    max_marks: Optional[float] = None
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


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    role: str
    created_at: datetime
