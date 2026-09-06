"""add task-level review execution ownership

Revision ID: 20260906_01
Revises: 20260905_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260906_01"
down_revision = "20260905_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_execution_runs",
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("identity_json", sa.JSON(), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("delivery_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index("ix_review_execution_runs_worker_id", "review_execution_runs", ["worker_id"])
    op.create_index(
        "ix_review_execution_runs_lease_expires_at",
        "review_execution_runs",
        ["lease_expires_at"],
    )
    op.create_index("ix_review_execution_runs_delivery_id", "review_execution_runs", ["delivery_id"])


def downgrade() -> None:
    op.drop_index("ix_review_execution_runs_delivery_id", table_name="review_execution_runs")
    op.drop_index("ix_review_execution_runs_lease_expires_at", table_name="review_execution_runs")
    op.drop_index("ix_review_execution_runs_worker_id", table_name="review_execution_runs")
    op.drop_table("review_execution_runs")
