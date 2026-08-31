"""add skills and report templates

Revision ID: 9f2b7f7f31ab
Revises: 4c280754c680
Create Date: 2026-03-15 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = '9f2b7f7f31ab'
down_revision = '4c280754c680'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'skills',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('slug', sa.String(length=160), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('source_type', sa.String(length=40), nullable=True),
        sa.Column('source_url', sa.Text(), nullable=True),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('tags', sa.JSON(), nullable=True),
        sa.Column('extension_manifest', sa.JSON(), nullable=True),
        sa.Column('extension_payload', sa.JSON(), nullable=True),
        sa.Column('is_system', sa.Boolean(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('created_by', sa.String(length=36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_skills_slug', 'skills', ['slug'], unique=True)
    op.create_table(
        'agent_skill_bindings',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('skill_id', sa.String(length=36), sa.ForeignKey('skills.id', ondelete='CASCADE'), nullable=False),
        sa.Column('agent_type', sa.String(length=40), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=True),
        sa.Column('always_include', sa.Boolean(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=True),
        sa.Column('match_keywords', sa.JSON(), nullable=True),
        sa.Column('match_config', sa.JSON(), nullable=True),
        sa.Column('created_by', sa.String(length=36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_agent_skill_bindings_skill_id', 'agent_skill_bindings', ['skill_id'], unique=False)
    op.create_index('ix_agent_skill_bindings_agent_type', 'agent_skill_bindings', ['agent_type'], unique=False)
    op.create_table(
        'report_templates',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('report_type', sa.String(length=40), nullable=True),
        sa.Column('output_format', sa.String(length=20), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('variables', sa.JSON(), nullable=True),
        sa.Column('is_default', sa.Boolean(), nullable=True),
        sa.Column('is_system', sa.Boolean(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=True),
        sa.Column('created_by', sa.String(length=36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        'agent_task_reports',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('task_id', sa.String(length=36), sa.ForeignKey('agent_tasks.id', ondelete='CASCADE'), nullable=False),
        sa.Column('template_id', sa.String(length=36), sa.ForeignKey('report_templates.id', ondelete='SET NULL'), nullable=True),
        sa.Column('output_format', sa.String(length=20), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('report_json', sa.JSON(), nullable=True),
        sa.Column('report_metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_agent_task_reports_task_id', 'agent_task_reports', ['task_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_agent_task_reports_task_id', table_name='agent_task_reports')
    op.drop_table('agent_task_reports')
    op.drop_table('report_templates')
    op.drop_index('ix_agent_skill_bindings_agent_type', table_name='agent_skill_bindings')
    op.drop_index('ix_agent_skill_bindings_skill_id', table_name='agent_skill_bindings')
    op.drop_table('agent_skill_bindings')
    op.drop_index('ix_skills_slug', table_name='skills')
    op.drop_table('skills')
