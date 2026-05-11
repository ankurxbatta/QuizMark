"""Add IngestJob table and source columns to questions

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    # New columns on questions for source traceability
    op.add_column("questions", sa.Column("source_page_range", sa.String(20), nullable=True))
    op.add_column("questions", sa.Column("source_chunk", sa.String(120), nullable=True))

    # New ingest_jobs table
    op.create_table(
        "ingest_jobs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("total_pages", sa.Integer(), default=0),
        sa.Column("question_type", sa.String(20), default="short_answer"),
        sa.Column("count_per_chapter", sa.Integer(), default=10),
        sa.Column("status", sa.String(20), nullable=False, default="queued"),
        sa.Column("chapters_done", sa.Integer(), default=0),
        sa.Column("questions_created", sa.Integer(), default=0),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("ingest_jobs")
    op.drop_column("questions", "source_chunk")
    op.drop_column("questions", "source_page_range")
