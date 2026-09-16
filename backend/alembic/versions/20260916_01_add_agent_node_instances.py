"""add persistent agent node instances

Revision ID: 20260916_01
Revises: 20260909_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260916_01"
down_revision = "20260909_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_node_instances",
        sa.Column("instance_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("agent_type", sa.String(length=64), nullable=False),
        sa.Column("entrypoint", sa.String(length=64), nullable=False),
        sa.Column("agent_version", sa.String(length=64), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("instance_id"),
    )
    op.create_index("ix_agent_node_instances_node_id", "agent_node_instances", ["node_id"])
    op.create_index(
        "ix_agent_node_instances_last_heartbeat_at",
        "agent_node_instances", ["last_heartbeat_at"],
    )
    op.add_column("review_execution_runs", sa.Column("node_id", sa.String(length=128), nullable=True))
    op.add_column("review_execution_runs", sa.Column("instance_id", sa.String(length=36), nullable=True))
    op.create_index("ix_review_execution_runs_node_id", "review_execution_runs", ["node_id"])
    op.create_index("ix_review_execution_runs_instance_id", "review_execution_runs", ["instance_id"])
    op.create_foreign_key(
        "fk_review_execution_runs_instance_id_agent_node_instances",
        "review_execution_runs", "agent_node_instances", ["instance_id"], ["instance_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_review_execution_runs_instance_id_agent_node_instances",
        "review_execution_runs", type_="foreignkey",
    )
    op.drop_index("ix_review_execution_runs_instance_id", table_name="review_execution_runs")
    op.drop_index("ix_review_execution_runs_node_id", table_name="review_execution_runs")
    op.drop_column("review_execution_runs", "instance_id")
    op.drop_column("review_execution_runs", "node_id")
    op.drop_index("ix_agent_node_instances_last_heartbeat_at", table_name="agent_node_instances")
    op.drop_index("ix_agent_node_instances_node_id", table_name="agent_node_instances")
    op.drop_table("agent_node_instances")
