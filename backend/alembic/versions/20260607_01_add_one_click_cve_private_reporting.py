"""add one click cve private vulnerability reporting flag

Revision ID: 20260607_01
Revises: 20260606_02
Create Date: 2026-06-07 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260607_01"
down_revision = "20260606_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "one_click_cve_batch_projects",
        sa.Column(
            "has_private_vulnerability_reporting",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("one_click_cve_batch_projects", "has_private_vulnerability_reporting")
