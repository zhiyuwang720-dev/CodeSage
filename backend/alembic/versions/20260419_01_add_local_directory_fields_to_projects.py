"""add local directory fields to projects

Revision ID: 20260419_01
Revises: 20260414_02
Create Date: 2026-04-19 12:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260419_01"
down_revision = "20260414_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("local_path", sa.String(), nullable=True))
    op.add_column("projects", sa.Column("workspace_mode", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "workspace_mode")
    op.drop_column("projects", "local_path")
