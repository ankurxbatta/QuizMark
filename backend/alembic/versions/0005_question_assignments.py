"""Add question assignments

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "question_assignments",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("question_id", sa.UUID(), sa.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("student_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("question_id", "student_id", name="uq_question_assignments_question_student"),
    )
    op.create_index("ix_question_assignments_question_id", "question_assignments", ["question_id"])
    op.create_index("ix_question_assignments_student_id", "question_assignments", ["student_id"])


def downgrade():
    op.drop_index("ix_question_assignments_student_id", table_name="question_assignments")
    op.drop_index("ix_question_assignments_question_id", table_name="question_assignments")
    op.drop_table("question_assignments")
