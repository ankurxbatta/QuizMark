from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
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


class QuestionAsset(BaseModel):
    kind: str
    caption: Optional[str] = None
    alt_text: Optional[str] = None
    table_html: Optional[str] = None
    image_id: Optional[str] = None
    source_page: Optional[int] = None


class QuestionOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: str
    question_text: str
    question_type: QuestionType
    model_answer: str
    rubric: str
    max_marks: float
    topic_tag: Optional[str] = None
    difficulty: Optional[Difficulty] = None
    # Structured answer key for objective questions (MCQ letter / "True"|"False").
    # Stored at generation time and used for deterministic marking — surface it so
    # the instructor view can show and edit the key instead of guessing it's null.
    correct_answer: Optional[str] = None
    book_id: Optional[str] = None
    chapter_num: Optional[int] = None
    source_page_range: Optional[str] = None
    source_chunk: Optional[str] = None
    assets: list[QuestionAsset] = Field(default_factory=list)
    assigned_student_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class AssessmentQuestionOut(BaseModel):
    id: str
    question_text: str
    question_type: QuestionType
    max_marks: float
    topic_tag: Optional[str] = None
    difficulty: Optional[Difficulty] = None
    source_page_range: Optional[str] = None
    source_chunk: Optional[str] = None
    assets: list[QuestionAsset] = Field(default_factory=list)
    created_at: datetime


class QuestionAssigneeUpdate(BaseModel):
    student_ids: list[str] = Field(default_factory=list)


class QuestionAssigneeOut(BaseModel):
    question_id: str
    student_ids: list[str] = Field(default_factory=list)


class QuizCreate(BaseModel):
    title: str
    description: Optional[str] = None
    question_ids: list[str] = Field(default_factory=list)


class QuizUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    question_ids: Optional[list[str]] = None


class QuizOut(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    question_ids: list[str] = Field(default_factory=list)
    question_count: int = 0
    assigned_student_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class QuizWithQuestions(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    questions: list[AssessmentQuestionOut] = Field(default_factory=list)


class QuizAssigneeUpdate(BaseModel):
    student_ids: list[str] = Field(default_factory=list)


class QuizAssigneeOut(BaseModel):
    quiz_id: str
    student_ids: list[str] = Field(default_factory=list)


class QuestionGenerateResponse(BaseModel):
    generated: int
    source_file: str
    source_pages: Optional[int] = None
    chunks_processed: Optional[int] = None
    topics_covered: Optional[list[str]] = None
    questions: list[QuestionOut]


class SubmissionCreate(BaseModel):
    question_id: str
    answer_text: str


class SubmissionOut(BaseModel):
    id: str
    student_id: str
    question_id: str
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
    id: str
    username: str
    role: str
    created_at: datetime
