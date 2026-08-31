"""add audit session runtime tables

Revision ID: 20260402_01
Revises: d4e5f6a7b8c9
Create Date: 2026-04-02 22:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260402_01"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("runtime_stack", sa.String(length=32), nullable=False, server_default="legacy"),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("recon_payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_sessions_project_id", "audit_sessions", ["project_id"], unique=False)
    op.create_index("ix_audit_sessions_task_id", "audit_sessions", ["task_id"], unique=False)

    op.create_table(
        "audit_session_messages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_session_messages_session_id", "audit_session_messages", ["session_id"], unique=False)

    op.create_table(
        "audit_session_turns",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_session_turns_session_id", "audit_session_turns", ["session_id"], unique=False)

    op.create_table(
        "audit_checkpoints",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_id", sa.String(length=36), sa.ForeignKey("audit_session_turns.id", ondelete="CASCADE"), nullable=True),
        sa.Column("checkpoint_type", sa.String(length=32), nullable=False, server_default="auto"),
        sa.Column("state_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_checkpoints_session_id", "audit_checkpoints", ["session_id"], unique=False)
    op.create_index("ix_audit_checkpoints_turn_id", "audit_checkpoints", ["turn_id"], unique=False)

    op.create_table(
        "audit_tool_calls",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_id", sa.String(length=36), sa.ForeignKey("audit_session_turns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("tool_use_id", sa.String(length=255), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("is_concurrency_safe", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_payload", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_audit_tool_calls_session_id", "audit_tool_calls", ["session_id"], unique=False)
    op.create_index("ix_audit_tool_calls_turn_id", "audit_tool_calls", ["turn_id"], unique=False)

    op.create_table(
        "audit_skills",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_ref", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(length=64), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("matched", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("skill_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_skills_session_id", "audit_skills", ["session_id"], unique=False)

    op.create_table(
        "audit_skill_invocations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_id", sa.String(length=36), sa.ForeignKey("audit_session_turns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("skill_ref", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="completed"),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_payload", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_skill_invocations_session_id", "audit_skill_invocations", ["session_id"], unique=False)
    op.create_index("ix_audit_skill_invocations_turn_id", "audit_skill_invocations", ["turn_id"], unique=False)

    op.create_table(
        "audit_memories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("memory_kind", sa.String(length=32), nullable=False, server_default="recall"),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("relevance_score", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_memories_session_id", "audit_memories", ["session_id"], unique=False)

    op.create_table(
        "audit_handoffs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target", sa.String(length=64), nullable=False, server_default="verification"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_handoffs_session_id", "audit_handoffs", ["session_id"], unique=False)

    op.create_table(
        "audit_artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_artifacts_session_id", "audit_artifacts", ["session_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_artifacts_session_id", table_name="audit_artifacts")
    op.drop_table("audit_artifacts")
    op.drop_index("ix_audit_handoffs_session_id", table_name="audit_handoffs")
    op.drop_table("audit_handoffs")
    op.drop_index("ix_audit_memories_session_id", table_name="audit_memories")
    op.drop_table("audit_memories")
    op.drop_index("ix_audit_skill_invocations_turn_id", table_name="audit_skill_invocations")
    op.drop_index("ix_audit_skill_invocations_session_id", table_name="audit_skill_invocations")
    op.drop_table("audit_skill_invocations")
    op.drop_index("ix_audit_skills_session_id", table_name="audit_skills")
    op.drop_table("audit_skills")
    op.drop_index("ix_audit_tool_calls_turn_id", table_name="audit_tool_calls")
    op.drop_index("ix_audit_tool_calls_session_id", table_name="audit_tool_calls")
    op.drop_table("audit_tool_calls")
    op.drop_index("ix_audit_checkpoints_turn_id", table_name="audit_checkpoints")
    op.drop_index("ix_audit_checkpoints_session_id", table_name="audit_checkpoints")
    op.drop_table("audit_checkpoints")
    op.drop_index("ix_audit_session_turns_session_id", table_name="audit_session_turns")
    op.drop_table("audit_session_turns")
    op.drop_index("ix_audit_session_messages_session_id", table_name="audit_session_messages")
    op.drop_table("audit_session_messages")
    op.drop_index("ix_audit_sessions_task_id", table_name="audit_sessions")
    op.drop_index("ix_audit_sessions_project_id", table_name="audit_sessions")
    op.drop_table("audit_sessions")
