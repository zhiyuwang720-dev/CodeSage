"""add audit_stages (09 L2 stage-level checkpoint)

Revision ID: 20260905_01
Revises: 20260711_01
Create Date: 2026-09-05 09:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260905_01"
down_revision = "20260711_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_stages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("stage_id", sa.String(length=120), nullable=False),
        sa.Column("stage_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_usage", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("findings_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state_payload", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "stage_type", name="uq_audit_stages_task_stage"),
    )
    op.create_index("ix_audit_stages_task_id", "audit_stages", ["task_id"])
    op.create_index("ix_audit_stages_stage_id", "audit_stages", ["stage_id"])
    op.create_index("ix_audit_stages_stage_type", "audit_stages", ["stage_type"])
    op.create_index("ix_audit_stages_session_id", "audit_stages", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_stages_session_id", table_name="audit_stages")
    op.drop_index("ix_audit_stages_stage_type", table_name="audit_stages")
    op.drop_index("ix_audit_stages_stage_id", table_name="audit_stages")
    op.drop_index("ix_audit_stages_task_id", table_name="audit_stages")
    op.drop_table("audit_stages")
