from pydantic import BaseModel, ConfigDict, Field
from typing import Literal, Optional
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


TimingMode = Literal["strict", "easy"]


class QuizCreate(BaseModel):
    title: str
    description: Optional[str] = None
    question_ids: list[str] = Field(default_factory=list)
    # Optional per-student timer: minutes allowed from the moment the student
    # presses Start. strict = hard cutoff, easy = warn + record lateness.
    time_limit_minutes: Optional[int] = Field(default=None, ge=1, le=600)
    timing_mode: TimingMode = "strict"


class QuizUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    question_ids: Optional[list[str]] = None
    # sentinel: field absent = leave unchanged; explicit null = remove timer
    time_limit_minutes: Optional[int] = Field(default=None, ge=1, le=600)
    timing_mode: Optional[TimingMode] = None


class QuizOut(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    question_ids: list[str] = Field(default_factory=list)
    question_count: int = 0
    assigned_student_ids: list[str] = Field(default_factory=list)
    time_limit_minutes: Optional[int] = None
    timing_mode: TimingMode = "strict"
    created_at: datetime


class QuizWithQuestions(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    time_limit_minutes: Optional[int] = None
    timing_mode: TimingMode = "strict"
    # For timed quizzes `questions` stays empty until the student starts an
    # attempt in the player, so question_count carries the size for the UI.
    question_count: int = 0
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


class QuizAttemptOut(BaseModel):
    id: str
    quiz_id: str
    student_id: str
    status: Literal["in_progress", "completed", "expired"]
    started_at: datetime
    deadline_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    late_by_seconds: int = 0
    draft_answers: dict[str, str] = Field(default_factory=dict)


class QuizPlayerMeta(BaseModel):
    """Pre-start lobby view of a quiz — no questions leaked before Start."""
    id: str
    title: str
    description: Optional[str] = None
    question_count: int = 0
    time_limit_minutes: Optional[int] = None
    timing_mode: TimingMode = "strict"


class QuizPlayerState(BaseModel):
    quiz: QuizPlayerMeta
    attempt: Optional[QuizAttemptOut] = None
    server_now: datetime


class SubmittedAnswerLite(BaseModel):
    submission_id: str
    answer_text: str
    is_marked: bool = False


class QuizAttemptStartOut(BaseModel):
    quiz: QuizPlayerMeta
    attempt: QuizAttemptOut
    questions: list[AssessmentQuestionOut] = Field(default_factory=list)
    # question_id → already-submitted answer (locked on the client)
    submitted: dict[str, SubmittedAnswerLite] = Field(default_factory=dict)
    server_now: datetime


class QuizDraftUpdate(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict)


class QuizAttemptRow(BaseModel):
    """Instructor view: one student's attempt with timing + progress."""
    attempt_id: str
    student_id: str
    username: str
    status: Literal["in_progress", "completed", "expired"]
    started_at: datetime
    deadline_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    late_by_seconds: int = 0
    answered_count: int = 0
    marked_count: int = 0
    total_questions: int = 0
    score: Optional[float] = None
    max_score: Optional[float] = None


class SubmissionCreate(BaseModel):
    question_id: str
    answer_text: str
    # Set by the quiz player so the timed-quiz deadline can be enforced.
    quiz_id: Optional[str] = None


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
    quiz_id: Optional[str] = None
    late_by_seconds: int = 0


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
