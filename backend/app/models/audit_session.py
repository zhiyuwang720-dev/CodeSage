from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


class AuditCheckpointType(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class AuditToolCallStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    INVALID = "invalid"
    MISSING = "missing"


class AuditSkillInvocationStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class AuditMemoryKind(StrEnum):
    INSTRUCTION = "instruction"
    RECALL = "recall"


class AuditSession(Base):
    __tablename__ = "audit_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), nullable=False, index=True)
    task_id = Column(String(36), nullable=True, index=True)
    runtime_stack = Column(String(32), nullable=False, default="legacy")
    state = Column(String(32), nullable=False, default="pending")
    system_prompt = Column(Text, nullable=True)
    recon_payload = Column(JSON, nullable=True)
    runtime_state_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    messages = relationship(
        "AuditSessionMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AuditSessionMessage.sequence",
    )
    turns = relationship(
        "AuditSessionTurn",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AuditSessionTurn.sequence",
    )
    model_stream_attempts = relationship(
        "AuditModelStreamAttempt",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AuditModelStreamAttempt.started_at",
    )
    checkpoints = relationship(
        "AuditCheckpoint",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AuditCheckpoint.created_at",
    )
    tool_calls = relationship(
        "AuditToolCall",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AuditToolCall.sequence",
    )
    skills = relationship(
        "AuditSkill",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AuditSkill.created_at",
    )
    skill_invocations = relationship(
        "AuditSkillInvocation",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AuditSkillInvocation.sequence",
    )
    memories = relationship(
        "AuditMemory",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AuditMemory.sequence",
    )


class AuditSessionMessage(Base):
    __tablename__ = "audit_session_messages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    role = Column(String(32), nullable=False)
    content = Column(Text, nullable=False)
    name = Column(String(255), nullable=True)
    message_metadata = Column("metadata", JSON, nullable=False, default=dict)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    session = relationship("AuditSession", back_populates="messages")


class AuditSessionTurn(Base):
    __tablename__ = "audit_session_turns"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    model_name = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False, default="open")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    session = relationship("AuditSession", back_populates="turns")
    checkpoints = relationship("AuditCheckpoint", back_populates="turn")
    tool_calls = relationship("AuditToolCall", back_populates="turn", order_by="AuditToolCall.sequence")
    skill_invocations = relationship("AuditSkillInvocation", back_populates="turn", order_by="AuditSkillInvocation.sequence")
    model_stream_attempts = relationship(
        "AuditModelStreamAttempt",
        back_populates="turn",
        order_by="AuditModelStreamAttempt.attempt_number",
    )


class AuditModelStreamAttempt(Base):
    __tablename__ = "audit_model_stream_attempts"

    id = Column(String(36), primary_key=True)
    session_id = Column(String(36), ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    turn_id = Column(String(36), ForeignKey("audit_session_turns.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_number = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, default="running")
    error_kind = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    provider_request_count = Column(Integer, nullable=False, default=1)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    session = relationship("AuditSession", back_populates="model_stream_attempts")
    turn = relationship("AuditSessionTurn", back_populates="model_stream_attempts")


class AuditCheckpoint(Base):
    __tablename__ = "audit_checkpoints"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    turn_id = Column(String(36), ForeignKey("audit_session_turns.id", ondelete="CASCADE"), nullable=True, index=True)
    checkpoint_type = Column(String(32), nullable=False, default=AuditCheckpointType.AUTO.value)
    state_payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    session = relationship("AuditSession", back_populates="checkpoints")
    turn = relationship("AuditSessionTurn", back_populates="checkpoints")


class AuditToolCall(Base):
    __tablename__ = "audit_tool_calls"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    turn_id = Column(String(36), ForeignKey("audit_session_turns.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    tool_use_id = Column(String(255), nullable=False)
    tool_name = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, default=AuditToolCallStatus.PENDING.value)
    is_concurrency_safe = Column(Boolean, nullable=False, default=False)
    input_payload = Column(JSON, nullable=False, default=dict)
    output_payload = Column(JSON, nullable=False, default=dict)
    error_message = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    session = relationship("AuditSession", back_populates="tool_calls")
    turn = relationship("AuditSessionTurn", back_populates="tool_calls")


class AuditSkill(Base):
    __tablename__ = "audit_skills"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    skill_ref = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    source_type = Column(String(64), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    matched = Column(Boolean, nullable=False, default=False)
    skill_metadata = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    session = relationship("AuditSession", back_populates="skills")


class AuditSkillInvocation(Base):
    __tablename__ = "audit_skill_invocations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    turn_id = Column(String(36), ForeignKey("audit_session_turns.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    skill_ref = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, default=AuditSkillInvocationStatus.COMPLETED.value)
    input_payload = Column(JSON, nullable=False, default=dict)
    output_payload = Column(JSON, nullable=False, default=dict)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    session = relationship("AuditSession", back_populates="skill_invocations")
    turn = relationship("AuditSessionTurn", back_populates="skill_invocations")


class AuditMemory(Base):
    __tablename__ = "audit_memories"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    memory_kind = Column(String(32), nullable=False, default=AuditMemoryKind.RECALL.value)
    title = Column(String(255), nullable=False)
    source_type = Column(String(64), nullable=False)
    source_ref = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    relevance_score = Column(Integer, nullable=True)
    metadata_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    session = relationship("AuditSession", back_populates="memories")


class AuditHandoff(Base):
    __tablename__ = "audit_handoffs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    target = Column(String(64), nullable=False, default="verification")
    status = Column(String(32), nullable=False, default="pending")
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditArtifact(Base):
    __tablename__ = "audit_artifacts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_type = Column(String(64), nullable=False)
    content = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
