"""任务级快速审查执行所有权。"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import relationship

from app.db.base import Base


class ReviewExecutionRun(Base):
    __tablename__ = "review_execution_runs"

    # 一任务只有一个稳定逻辑 run；run_id 在 identity_json 内且 resume 不变。
    task_id = Column(
        String(36), ForeignKey("agent_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    identity_json = Column(JSON, nullable=False)
    attempt_id = Column(String(36), nullable=True)
    worker_id = Column(String(255), nullable=True, index=True)
    lease_epoch = Column(Integer, nullable=False, default=0)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    delivery_id = Column(String(36), nullable=False, index=True)

    task = relationship("AgentTask", uselist=False)

