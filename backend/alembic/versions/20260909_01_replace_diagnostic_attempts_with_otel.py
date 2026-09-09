"""replace diagnostic model attempts with OpenTelemetry

Revision ID: 20260909_01
Revises: 20260907_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260909_01"
down_revision = "20260907_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("audit_model_stream_attempts")
    op.drop_column("audit_tool_calls", "duration_ms")


def downgrade() -> None:
    # Diagnostic history is intentionally not recoverable; downgrade creates an empty table.
    op.add_column("audit_tool_calls", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.create_table(
        "audit_model_stream_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("error_kind", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("provider_request_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["audit_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["audit_session_turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_model_stream_attempts_session_id",
        "audit_model_stream_attempts",
        ["session_id"],
    )
    op.create_index(
        "ix_audit_model_stream_attempts_turn_id",
        "audit_model_stream_attempts",
        ["turn_id"],
    )
