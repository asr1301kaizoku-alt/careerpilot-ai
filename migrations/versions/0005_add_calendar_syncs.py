"""Add generic calendar sync records and migrate interview event IDs.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    # Keep a copy of legacy IDs until the new sync rows have been inserted.
    op.create_table(
        "_calendar_sync_legacy_event_ids",
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("external_event_id", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("application_id"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO _calendar_sync_legacy_event_ids (
                application_id, external_event_id
            )
            SELECT id, google_calendar_event_id
            FROM applications
            WHERE google_calendar_event_id IS NOT NULL
            """
        )
    )

    # Supported SQLite versions can drop this column without rebuilding the
    # parent table. A batch rebuild would trigger CASCADE on checklist_items.
    op.drop_column("applications", "google_calendar_event_id")

    op.create_table(
        "calendar_syncs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=True),
        sa.Column("checklist_item_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("calendar_id", sa.String(length=255), nullable=False),
        sa.Column("external_event_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "(application_id IS NOT NULL AND checklist_item_id IS NULL) "
            "OR (application_id IS NULL AND checklist_item_id IS NOT NULL)",
            name="ck_calendar_sync_exactly_one_owner",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["checklist_item_id"],
            ["checklist_items.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "application_id",
            "event_type",
            "provider",
            name="uq_calendar_sync_application_event_provider",
        ),
        sa.UniqueConstraint(
            "checklist_item_id",
            "event_type",
            "provider",
            name="uq_calendar_sync_checklist_event_provider",
        ),
    )
    op.create_index(
        op.f("ix_calendar_syncs_application_id"),
        "calendar_syncs",
        ["application_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_calendar_syncs_checklist_item_id"),
        "calendar_syncs",
        ["checklist_item_id"],
        unique=False,
    )

    op.execute(
        sa.text(
            """
            INSERT INTO calendar_syncs (
                application_id,
                checklist_item_id,
                event_type,
                provider,
                calendar_id,
                external_event_id,
                created_at,
                updated_at
            )
            SELECT
                application_id,
                NULL,
                'interview',
                'google',
                'primary',
                external_event_id,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM _calendar_sync_legacy_event_ids
            """
        )
    )
    op.drop_table("_calendar_sync_legacy_event_ids")


def downgrade():
    op.add_column(
        "applications",
        sa.Column(
            "google_calendar_event_id",
            sa.String(length=255),
            nullable=True,
        )
    )

    op.execute(
        sa.text(
            """
            UPDATE applications
            SET google_calendar_event_id = (
                SELECT calendar_syncs.external_event_id
                FROM calendar_syncs
                WHERE calendar_syncs.application_id = applications.id
                  AND calendar_syncs.event_type = 'interview'
                  AND calendar_syncs.provider = 'google'
                LIMIT 1
            )
            """
        )
    )

    op.drop_index(
        op.f("ix_calendar_syncs_checklist_item_id"),
        table_name="calendar_syncs",
    )
    op.drop_index(
        op.f("ix_calendar_syncs_application_id"),
        table_name="calendar_syncs",
    )
    op.drop_table("calendar_syncs")
