"""add runtime_state_json to audit_sessions

Revision ID: 20260413_01
Revises: 20260402_01
Create Date: 2026-04-13 16:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260413_01"
down_revision = "20260402_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_sessions",
        sa.Column(
            "runtime_state_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.alter_column("audit_sessions", "runtime_state_json", server_default=None)


def downgrade() -> None:
    op.drop_column("audit_sessions", "runtime_state_json")
