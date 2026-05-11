"""Initial schema with pgvector

Revision ID: 0001
Revises:
Create Date: 2026-05-09
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("username", sa.String(100), unique=True, nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), default=0),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "questions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("question_type", sa.String(20), nullable=False),
        sa.Column("model_answer", sa.Text(), nullable=False),
        sa.Column("rubric", sa.Text(), nullable=False),
        sa.Column("max_marks", sa.Float(), nullable=False),
        sa.Column("topic_tag", sa.String(100)),
        sa.Column("difficulty", sa.String(20)),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "submissions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("student_id", sa.UUID(), sa.ForeignKey("users.id")),
        sa.Column("question_id", sa.UUID(), sa.ForeignKey("questions.id")),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("auto_mark", sa.Float(), nullable=True),
        sa.Column("auto_feedback", sa.Text(), nullable=True),
        sa.Column("override_mark", sa.Float(), nullable=True),
        sa.Column("override_feedback", sa.Text(), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("is_flagged", sa.Boolean(), default=False),
        sa.Column("is_marked", sa.Boolean(), default=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("marked_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("submission_id", sa.UUID(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("audit_logs")
    op.drop_table("submissions")
    op.drop_table("questions")
    op.drop_table("users")
