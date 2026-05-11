"""Add hybrid pipeline columns to submissions

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("submissions", sa.Column("auto_confidence", sa.Float(), nullable=True))
    op.add_column("submissions", sa.Column("marking_route", sa.String(10), nullable=True))
    op.add_column("submissions", sa.Column("slm_keyword_coverage", sa.Float(), nullable=True))
    op.add_column("submissions", sa.Column("slm_semantic_sim", sa.Float(), nullable=True))
    op.add_column("submissions", sa.Column("slm_raw_score", sa.Float(), nullable=True))


def downgrade():
    op.drop_column("submissions", "slm_raw_score")
    op.drop_column("submissions", "slm_semantic_sim")
    op.drop_column("submissions", "slm_keyword_coverage")
    op.drop_column("submissions", "marking_route")
    op.drop_column("submissions", "auto_confidence")
