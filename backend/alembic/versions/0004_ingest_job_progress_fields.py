"""Add progress tracking fields to ingest_jobs

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ingest_jobs", sa.Column("total_chapters", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ingest_jobs", sa.Column("current_chapter", sa.Integer(), nullable=True))
    op.add_column("ingest_jobs", sa.Column("current_chapter_title", sa.String(length=255), nullable=True))
    op.add_column("ingest_jobs", sa.Column("progress_message", sa.Text(), nullable=True))
    op.add_column("ingest_jobs", sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE ingest_jobs SET total_chapters = COALESCE(total_chapters, 0)")
    op.alter_column("ingest_jobs", "total_chapters", server_default=None)


def downgrade():
    op.drop_column("ingest_jobs", "last_heartbeat_at")
    op.drop_column("ingest_jobs", "progress_message")
    op.drop_column("ingest_jobs", "current_chapter_title")
    op.drop_column("ingest_jobs", "current_chapter")
    op.drop_column("ingest_jobs", "total_chapters")
