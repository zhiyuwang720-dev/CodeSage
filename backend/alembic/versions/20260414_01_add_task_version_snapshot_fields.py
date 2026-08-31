"""add task version snapshot fields

Revision ID: 20260414_01
Revises: 20260413_01
Create Date: 2026-04-14 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260414_01"
down_revision = "20260413_01"
branch_labels = None
depends_on = None


LEGACY_VERSION_LABEL = "legacy-task"


def upgrade() -> None:
    op.add_column("agent_tasks", sa.Column("version_label", sa.String(length=255), nullable=True))
    op.add_column("agent_tasks", sa.Column("version_tag", sa.String(length=255), nullable=True))
    op.add_column("agent_tasks", sa.Column("commit_sha", sa.String(length=64), nullable=True))
    op.add_column("agent_tasks", sa.Column("repository_url_snapshot", sa.Text(), nullable=True))

    op.execute(
        sa.text(
            """
            UPDATE agent_tasks
            SET version_label = COALESCE(NULLIF(branch_name, ''), :fallback)
            WHERE version_label IS NULL
            """
        ).bindparams(fallback=LEGACY_VERSION_LABEL)
    )
    op.alter_column("agent_tasks", "version_label", existing_type=sa.String(length=255), nullable=False)


def downgrade() -> None:
    op.drop_column("agent_tasks", "repository_url_snapshot")
    op.drop_column("agent_tasks", "commit_sha")
    op.drop_column("agent_tasks", "version_tag")
    op.drop_column("agent_tasks", "version_label")
