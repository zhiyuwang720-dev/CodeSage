"""09: 审计阶段级 CheckPoint(L2)契约。

AuditStage = 单条 stage 记录(每视角/每阶段一行, state_payload 存阶段产物快照);
AuditStageStore = 读写契约(register/start/complete/fail/list/completed/get/touch_session)。

stage_type 一套字面量兼容:
- 快速: intake / review:security|architecture|quality / critic(预留) / report
- 深度: intake / anatomy / planning / review:<dimension_id>(动态, planning 后登记) / critic / report
critic 类型已定义但当前 quick 模式不登记不写入(planned_stages 仅 5 项)。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class StageStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class AuditStage(BaseModel):
    """阶段级 checkpoint 记录(L2 核心类型)。"""

    stage_id: str  # 幂等键 = f"{task_id}:{stage_type}"
    task_id: str
    stage_type: str
    status: StageStatus
    session_id: str | None = None  # L3 会话续跑锚点
    turn_count: int = 0
    token_usage: int = 0
    tool_calls: int = 0
    findings_count: int = 0
    state_payload: dict[str, Any] = Field(default_factory=dict)  # 阶段产物快照(含 findings)
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@runtime_checkable
class AuditStageStore(Protocol):
    """audit_stages 读写契约。所有写按 (task_id, stage_type) 幂等 upsert。"""

    async def register(
        self, db, task_id: str, planned_stages: list[str]
    ) -> None: ...

    async def start(
        self, db, task_id: str, stage_type: str, *, session_id: str | None = None
    ) -> AuditStage: ...

    async def complete(
        self,
        db,
        task_id: str,
        stage_type: str,
        *,
        session_id: str | None = None,
        stats: dict[str, Any] | None = None,
        findings: list[dict[str, Any]] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditStage: ...

    async def fail(self, db, task_id: str, stage_type: str, error: str) -> AuditStage: ...

    async def list(self, db, task_id: str) -> list[AuditStage]: ...

    async def completed(self, db, task_id: str) -> list[AuditStage]: ...

    async def get(self, db, task_id: str, stage_type: str) -> AuditStage | None: ...

    async def touch_session(
        self, db, task_id: str, stage_type: str, session_id: str
    ) -> None: ...
