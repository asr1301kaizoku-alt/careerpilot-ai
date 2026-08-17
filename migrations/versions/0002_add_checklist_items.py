"""Add per-application checklist items.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "checklist_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=150), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("is_completed", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_checklist_items_application_id"),
        "checklist_items",
        ["application_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_checklist_items_is_completed"),
        "checklist_items",
        ["is_completed"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_checklist_items_is_completed"),
        table_name="checklist_items",
    )
    op.drop_index(
        op.f("ix_checklist_items_application_id"),
        table_name="checklist_items",
    )
    op.drop_table("checklist_items")
