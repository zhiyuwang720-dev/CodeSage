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
    node_id = Column(String(128), nullable=True, index=True)
    instance_id = Column(
        String(36), ForeignKey("agent_node_instances.instance_id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    lease_epoch = Column(Integer, nullable=False, default=0)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    delivery_id = Column(String(36), nullable=False, index=True)

    task = relationship("AgentTask", uselist=False)


class AgentNodeInstance(Base):
    """A concrete process serving one allowlisted agent node."""

    __tablename__ = "agent_node_instances"

    instance_id = Column(String(36), primary_key=True)
    node_id = Column(String(128), nullable=False, index=True)
    agent_type = Column(String(64), nullable=False)
    entrypoint = Column(String(64), nullable=False)
    agent_version = Column(String(64), nullable=False)
    protocol_version = Column(Integer, nullable=False)
    capabilities = Column(JSON, nullable=False, default=list)
    max_concurrency = Column(Integer, nullable=False)
    pid = Column(Integer, nullable=False)
    hostname = Column(String(255), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=False, index=True)
    stopped_at = Column(DateTime(timezone=True), nullable=True)

