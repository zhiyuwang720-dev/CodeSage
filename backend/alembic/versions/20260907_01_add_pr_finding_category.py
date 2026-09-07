"""add canonical PR finding category

Revision ID: 20260907_01
Revises: 20260906_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260907_01"
down_revision = "20260906_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_findings", sa.Column("category", sa.String(length=100), nullable=True))
    op.create_index("ix_agent_findings_category", "agent_findings", ["category"])
    # 历史物理列保留，但普通 PR 发现不再被迫伪装成漏洞类型。
    op.alter_column(
        "agent_findings",
        "vulnerability_type",
        existing_type=sa.String(length=100),
        nullable=True,
    )


def downgrade() -> None:
    # 降级时只为缺值行恢复可满足旧约束的占位值，不覆盖历史原值。
    op.execute(
        "UPDATE agent_findings SET vulnerability_type = 'other' "
        "WHERE vulnerability_type IS NULL"
    )
    op.alter_column(
        "agent_findings",
        "vulnerability_type",
        existing_type=sa.String(length=100),
        nullable=False,
    )
    op.drop_index("ix_agent_findings_category", table_name="agent_findings")
    op.drop_column("agent_findings", "category")
