"""add metadata_json to report_templates

Revision ID: c8f9ab12d001
Revises: b31d2b5f6a8c
Create Date: 2026-03-15 18:30:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = 'c8f9ab12d001'
down_revision = 'b31d2b5f6a8c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('report_templates', sa.Column('metadata_json', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('report_templates', 'metadata_json')
