"""add one click cve tables

Revision ID: 20260606_01
Revises: 20260527_01
Create Date: 2026-06-06 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260606_01"
down_revision = "20260527_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "one_click_cve_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("found_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("current_step", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_one_click_cve_batches_created_at", "one_click_cve_batches", ["created_at"], unique=False)
    op.create_index("ix_one_click_cve_batches_status", "one_click_cve_batches", ["status"], unique=False)
    op.create_index("ix_one_click_cve_batches_user_id", "one_click_cve_batches", ["user_id"], unique=False)

    op.create_table(
        "one_click_cve_batch_projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("agent_task_id", sa.String(length=36), nullable=True),
        sa.Column("github_full_name", sa.String(length=255), nullable=False),
        sa.Column("repository_url", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=80), nullable=True),
        sa.Column("stars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("default_branch", sa.String(length=255), nullable=True),
        sa.Column("has_security_advisory", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("advisory_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("has_security_policy", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="candidate"),
        sa.Column("findings_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at_local", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_task_id"], ["agent_tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["batch_id"], ["one_click_cve_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_one_click_cve_batch_projects_agent_task_id", "one_click_cve_batch_projects", ["agent_task_id"], unique=False)
    op.create_index("ix_one_click_cve_batch_projects_batch_id", "one_click_cve_batch_projects", ["batch_id"], unique=False)
    op.create_index("ix_one_click_cve_batch_projects_created_at", "one_click_cve_batch_projects", ["created_at"], unique=False)
    op.create_index("ix_one_click_cve_batch_projects_github_full_name", "one_click_cve_batch_projects", ["github_full_name"], unique=False)
    op.create_index("ix_one_click_cve_batch_projects_project_id", "one_click_cve_batch_projects", ["project_id"], unique=False)
    op.create_index("ix_one_click_cve_batch_projects_status", "one_click_cve_batch_projects", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_one_click_cve_batch_projects_status", table_name="one_click_cve_batch_projects")
    op.drop_index("ix_one_click_cve_batch_projects_project_id", table_name="one_click_cve_batch_projects")
    op.drop_index("ix_one_click_cve_batch_projects_github_full_name", table_name="one_click_cve_batch_projects")
    op.drop_index("ix_one_click_cve_batch_projects_created_at", table_name="one_click_cve_batch_projects")
    op.drop_index("ix_one_click_cve_batch_projects_batch_id", table_name="one_click_cve_batch_projects")
    op.drop_index("ix_one_click_cve_batch_projects_agent_task_id", table_name="one_click_cve_batch_projects")
    op.drop_table("one_click_cve_batch_projects")
    op.drop_index("ix_one_click_cve_batches_user_id", table_name="one_click_cve_batches")
    op.drop_index("ix_one_click_cve_batches_status", table_name="one_click_cve_batches")
    op.drop_index("ix_one_click_cve_batches_created_at", table_name="one_click_cve_batches")
    op.drop_table("one_click_cve_batches")
