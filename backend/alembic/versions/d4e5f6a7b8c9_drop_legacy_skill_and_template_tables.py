"""drop legacy skill and template tables

Revision ID: d4e5f6a7b8c9
Revises: c8f9ab12d001
Create Date: 2026-03-17 21:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c8f9ab12d001"
branch_labels = None
depends_on = None


def _drop_fk_if_exists(table_name: str, referred_table: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys(table_name):
        if fk.get("referred_table") == referred_table and fk.get("name"):
            op.drop_constraint(fk["name"], table_name, type_="foreignkey")


def _drop_table_if_exists(table_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name in inspector.get_table_names():
        op.drop_table(table_name)


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    index_names = {item["name"] for item in inspector.get_indexes(table_name)}
    if index_name in index_names:
        op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "agent_task_reports" in inspector.get_table_names():
        _drop_fk_if_exists("agent_task_reports", "report_templates")
        with op.batch_alter_table("agent_task_reports") as batch_op:
            batch_op.alter_column(
                "template_id",
                existing_type=sa.String(length=36),
                type_=sa.String(length=160),
                existing_nullable=True,
            )

    if "agent_skill_bindings" in inspector.get_table_names():
        _drop_index_if_exists("agent_skill_bindings", "ix_agent_skill_bindings_agent_type")
        _drop_index_if_exists("agent_skill_bindings", "ix_agent_skill_bindings_skill_id")
        _drop_table_if_exists("agent_skill_bindings")

    if "skills" in inspector.get_table_names():
        _drop_index_if_exists("skills", "ix_skills_slug")
        _drop_table_if_exists("skills")

    if "report_templates" in inspector.get_table_names():
        _drop_table_if_exists("report_templates")


def downgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("extension_manifest", sa.JSON(), nullable=True),
        sa.Column("extension_payload", sa.JSON(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_skills_slug", "skills", ["slug"], unique=True)

    op.create_table(
        "agent_skill_bindings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("skill_id", sa.String(length=36), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_type", sa.String(length=40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("always_include", sa.Boolean(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("match_keywords", sa.JSON(), nullable=True),
        sa.Column("match_config", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_skill_bindings_skill_id", "agent_skill_bindings", ["skill_id"], unique=False)
    op.create_index("ix_agent_skill_bindings_agent_type", "agent_skill_bindings", ["agent_type"], unique=False)

    op.create_table(
        "report_templates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("report_type", sa.String(length=40), nullable=True),
        sa.Column("output_format", sa.String(length=20), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("variables", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    with op.batch_alter_table("agent_task_reports") as batch_op:
        batch_op.alter_column(
            "template_id",
            existing_type=sa.String(length=160),
            type_=sa.String(length=36),
            existing_nullable=True,
        )
        batch_op.create_foreign_key(None, "report_templates", ["template_id"], ["id"], ondelete="SET NULL")
