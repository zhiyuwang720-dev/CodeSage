"""09: 审计阶段级 CheckPoint(L2)契约。

AuditStage = 单条 stage 记录(每视角/每阶段一行, state_payload 存阶段产物快照);
AuditStageStore = 读写契约(register/start/complete/fail/list/completed/get/touch_session)。

stage_type 是一套自由字符串字面量, 兼容快速/深度两套序列(stage 框架与 stage 图无关,
深度审计 planning 完成后按 ReviewPlan.dimensions 动态登记 review:<dim> 即可):
- 快速(planned_stages 仅 5 项):
  intake / review:security|architecture|quality / report   (critic 预留不登记)
- 深度(plan 07 定义, 纯计划态):
  intake / anatomy / planning / review:<dimension_id>×N / critic / report

resume 消费按前缀约定工作:
- completed 的 review:* stage → 读 state_payload["findings"] 快照零 LLM 预填;
- pending/running/failed 且带 session_id → L3 会话续跑;
- critic/report 完成 → 跳过重跑直接出终态。

预留 payload schema(接口+字段落点, 不实现; 深度审计 / plan 10 Critic 落地时
直接复用本契约, stage 框架零改动):
- planning:  state_payload["review_plan"] = {"dimensions": [...], "strategy": {...}} —
  深度审计 planning 完成后据此登记 review:<dim> 序列, resume 时重建维度。
- critic:    state_payload = {
      "session_id": str, "findings_count": int,
      "verdicts": [{"finding_id": str, "action": str, "severity_delta": int, "reason": str}],
  }
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
