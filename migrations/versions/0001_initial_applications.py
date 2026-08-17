"""Create the Phase 1 applications table.

Revision ID: 0001
Revises:
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_name", sa.String(length=100), nullable=False),
        sa.Column("position_name", sa.String(length=150), nullable=True),
        sa.Column("application_url", sa.String(length=500), nullable=True),
        sa.Column("application_source", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("es_deadline", sa.DateTime(), nullable=True),
        sa.Column("web_test_deadline", sa.DateTime(), nullable=True),
        sa.Column("interview_at", sa.DateTime(), nullable=True),
        sa.Column("interview_format", sa.String(length=50), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("applications")
