"""add email calendar registrations

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-16
"""

from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "email_calendar_registrations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_key", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("connection_key", sa.String(length=64), nullable=False),
        sa.Column("message_key", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("calendar_id", sa.String(length=255), nullable=False),
        sa.Column("external_event_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_key",
            "provider",
            "connection_key",
            "message_key",
            "event_type",
            name="uq_email_calendar_registration_source",
        ),
    )
    op.create_index(
        op.f("ix_email_calendar_registrations_message_key"),
        "email_calendar_registrations",
        ["message_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_calendar_registrations_owner_key"),
        "email_calendar_registrations",
        ["owner_key"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_email_calendar_registrations_owner_key"),
        table_name="email_calendar_registrations",
    )
    op.drop_index(
        op.f("ix_email_calendar_registrations_message_key"),
        table_name="email_calendar_registrations",
    )
    op.drop_table("email_calendar_registrations")
