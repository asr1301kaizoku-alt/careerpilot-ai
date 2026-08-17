"""Add Google Calendar event ID to applications.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "applications",
        sa.Column(
            "google_calendar_event_id",
            sa.String(length=255),
            nullable=True,
        ),
    )


def downgrade():
    with op.batch_alter_table("applications") as batch_op:
        batch_op.drop_column("google_calendar_event_id")
