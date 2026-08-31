"""merge files-findings and skills-report heads

Revision ID: b31d2b5f6a8c
Revises: 008_add_files_with_findings, 9f2b7f7f31ab
Create Date: 2026-03-15 14:45:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'b31d2b5f6a8c'
down_revision = ('008_add_files_with_findings', '9f2b7f7f31ab')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
