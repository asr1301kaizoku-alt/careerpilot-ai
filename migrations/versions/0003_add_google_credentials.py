"""Add Google OAuth credential storage.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "google_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_key", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("google_account_email", sa.String(length=255), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_uri", sa.String(length=500), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_key",
            "provider",
            name="uq_google_credentials_owner_provider",
        ),
    )
    op.create_index(
        op.f("ix_google_credentials_owner_key"),
        "google_credentials",
        ["owner_key"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_google_credentials_owner_key"),
        table_name="google_credentials",
    )
    op.drop_table("google_credentials")
