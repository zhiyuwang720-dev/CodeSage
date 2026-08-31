"""add checkmarx scan tables

Revision ID: 20260527_01
Revises: 20260419_01
Create Date: 2026-05-27 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260527_01"
down_revision = "20260419_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "checkmarx_scan_jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("current_step", sa.String(), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("project_name", sa.String(), nullable=False),
        sa.Column("source_filename", sa.String(), nullable=False),
        sa.Column("checkmarx_base_url", sa.String(), nullable=True),
        sa.Column("checkmarx_project_id", sa.String(), nullable=True),
        sa.Column("scan_id", sa.String(), nullable=True),
        sa.Column("totals_json", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_checkmarx_scan_jobs_created_by", "checkmarx_scan_jobs", ["created_by"], unique=False)
    op.create_index("ix_checkmarx_scan_jobs_scan_id", "checkmarx_scan_jobs", ["scan_id"], unique=False)
    op.create_index("ix_checkmarx_scan_jobs_status", "checkmarx_scan_jobs", ["status"], unique=False)

    op.create_table(
        "checkmarx_scan_results",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("scan_id", sa.String(), nullable=False),
        sa.Column("path_id", sa.String(), nullable=False),
        sa.Column("vulnerability", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("severity", sa.Integer(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("ai_judgement", sa.Boolean(), nullable=True),
        sa.Column("ai_reason", sa.Text(), nullable=True),
        sa.Column("raw_result", sa.Text(), nullable=True),
        sa.Column("workflow_response", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["checkmarx_scan_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_checkmarx_scan_results_job_id", "checkmarx_scan_results", ["job_id"], unique=False)
    op.create_index("ix_checkmarx_scan_results_path_id", "checkmarx_scan_results", ["path_id"], unique=False)
    op.create_index("ix_checkmarx_scan_results_scan_id", "checkmarx_scan_results", ["scan_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_checkmarx_scan_results_scan_id", table_name="checkmarx_scan_results")
    op.drop_index("ix_checkmarx_scan_results_path_id", table_name="checkmarx_scan_results")
    op.drop_index("ix_checkmarx_scan_results_job_id", table_name="checkmarx_scan_results")
    op.drop_table("checkmarx_scan_results")
    op.drop_index("ix_checkmarx_scan_jobs_status", table_name="checkmarx_scan_jobs")
    op.drop_index("ix_checkmarx_scan_jobs_scan_id", table_name="checkmarx_scan_jobs")
    op.drop_index("ix_checkmarx_scan_jobs_created_by", table_name="checkmarx_scan_jobs")
    op.drop_table("checkmarx_scan_jobs")
