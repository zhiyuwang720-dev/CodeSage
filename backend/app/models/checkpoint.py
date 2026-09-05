"""09: 审计阶段级 CheckPoint(L2)ORM 模型。

`audit_stages` 表: 每 stage 一行, (task_id, stage_type) 唯一约束 = 幂等 upsert 依据
(重跑/恢复不产生重复行)。stage_type 对深度审计的 `review:<dimension_id>` 是动态字符串,
一套表兼容快速(intake/review:*×3/report)与深度(intake/anatomy/planning/review:<dim>/critic/report)。

不复用 AutoCVE 遗留 `agent_checkpoints`(其 agent_id/parent_agent_id/iteration 是 Agent 树形态,
与"阶段"概念不符; 该表留给将来 Agent 树可视化)。
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.db.base import Base


class AuditStageORM(Base):
    __tablename__ = "audit_stages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(
        String(36), ForeignKey("agent_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 幂等键 f"{task_id}:{stage_type}"; 冗余列便于按 stage 前缀查询(review:%)
    stage_id = Column(String(120), nullable=False, index=True)
    stage_type = Column(String(50), nullable=False, index=True)  # intake|anatomy|planning|review:*|critic|report
    status = Column(String(20), nullable=False, default="pending")  # pending|running|completed|failed
    session_id = Column(String(36), nullable=True, index=True)  # L3 会话续跑锚点
    turn_count = Column(Integer, nullable=False, default=0)
    token_usage = Column(Integer, nullable=False, default=0)
    tool_calls = Column(Integer, nullable=False, default=0)
    findings_count = Column(Integer, nullable=False, default=0)
    state_payload = Column(JSON, nullable=False, default=dict)  # 阶段产物快照(含 findings)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("task_id", "stage_type", name="uq_audit_stages_task_stage"),
    )
