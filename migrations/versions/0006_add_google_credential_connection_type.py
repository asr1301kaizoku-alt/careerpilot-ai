"""Separate Google credentials by connection purpose.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-08
"""

from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


OLD_UNIQUE_CONSTRAINT = "uq_google_credentials_owner_provider"
NEW_UNIQUE_CONSTRAINT = (
    "uq_google_credentials_owner_provider_connection"
)
CONNECTION_CHECK_CONSTRAINT = "ck_google_credentials_connection_type"


def upgrade():
    with op.batch_alter_table("google_credentials") as batch_op:
        batch_op.add_column(
            sa.Column(
                "connection_type",
                sa.String(length=50),
                nullable=False,
                server_default="calendar",
            )
        )
        batch_op.drop_constraint(
            OLD_UNIQUE_CONSTRAINT,
            type_="unique",
        )
        batch_op.create_check_constraint(
            CONNECTION_CHECK_CONSTRAINT,
            "connection_type IN ('calendar', 'gmail')",
        )
        batch_op.create_unique_constraint(
            NEW_UNIQUE_CONSTRAINT,
            ["owner_key", "provider", "connection_type"],
        )


def downgrade():
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            """
            SELECT owner_key, provider
            FROM google_credentials
            GROUP BY owner_key, provider
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot downgrade while multiple Google connection types "
            "exist for the same owner and provider."
        )

    with op.batch_alter_table("google_credentials") as batch_op:
        batch_op.drop_constraint(
            NEW_UNIQUE_CONSTRAINT,
            type_="unique",
        )
        batch_op.drop_constraint(
            CONNECTION_CHECK_CONSTRAINT,
            type_="check",
        )
        batch_op.drop_column("connection_type")
        batch_op.create_unique_constraint(
            OLD_UNIQUE_CONSTRAINT,
            ["owner_key", "provider"],
        )
