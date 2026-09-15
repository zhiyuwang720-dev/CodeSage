"""Cached task report model."""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


class AgentTaskReport(Base):
    __tablename__ = "agent_task_reports"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String(36), ForeignKey("agent_tasks.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    template_id = Column(String(160), nullable=True)
    output_format = Column(String(20), default="markdown")
    title = Column(String(255), nullable=True)
    content = Column(Text, nullable=False)
    report_json = Column(JSON, nullable=True)
    report_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    task = relationship("AgentTask")
