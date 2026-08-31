"""add one click cve project version fields

Revision ID: 20260606_02
Revises: 20260606_01
Create Date: 2026-06-06 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260606_02"
down_revision = "20260606_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("one_click_cve_batch_projects", sa.Column("version_label", sa.String(length=255), nullable=True))
    op.add_column("one_click_cve_batch_projects", sa.Column("version_source", sa.String(length=50), nullable=True))
    op.create_index("ix_one_click_cve_batch_projects_version_label", "one_click_cve_batch_projects", ["version_label"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_one_click_cve_batch_projects_version_label", table_name="one_click_cve_batch_projects")
    op.drop_column("one_click_cve_batch_projects", "version_source")
    op.drop_column("one_click_cve_batch_projects", "version_label")
