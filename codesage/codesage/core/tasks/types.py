"""任务契约层:Task/TaskSummary/TaskStatus/TaskUpdate。

字段一律 snake_case(源码与任务文件 JSON 均如此,镜像 core/session.py 风格);
spec §3.2 的 camelCase JSON 样例是 Kode 对照,实现以 snake_case 为准。
deleted 不是状态(§3.4):状态机仍三态,删除是 TaskUpdate 的工具面动作,
由 storage(S2)在工具入口转换为文件删除。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """任务三态;completed 是终态(§6.2),不允许回退。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class Task(BaseModel):
    """一条任务:状态、owner、双向依赖(blocks/blocked_by)与任意 metadata。"""

    id: str
    subject: str
    description: str
    active_form: str | None = None  # 进行时展示用(如 spinner 文案)
    status: TaskStatus = TaskStatus.PENDING
    owner: str | None = None
    blocks: list[str] = Field(default_factory=list)  # 本任务阻塞的任务 id
    blocked_by: list[str] = Field(default_factory=list)  # 阻塞本任务的任务 id
    metadata: dict = Field(default_factory=dict)


class TaskSummary(BaseModel):
    """任务摘要(TaskList 输出);blocked_by 只保留「现存且未完成」的阻塞者(§6.4)。"""

    id: str
    subject: str
    status: TaskStatus
    owner: str | None = None
    blocked_by: list[str] = Field(default_factory=list)


class TaskUpdate(BaseModel):
    """TaskUpdate 工具的输入模型(§6.3)。

    status 传 "deleted"(TaskStatus 之外的动作值)由工具面处理,
    不落此模型(S2 起生效)。
    """

    task_id: str
    subject: str | None = None
    description: str | None = None
    active_form: str | None = None
    status: TaskStatus | None = None
    add_blocks: list[str] = Field(default_factory=list)  # 增量添加,不提供 remove
    add_blocked_by: list[str] = Field(default_factory=list)
    owner: str | None = None
    metadata: dict | None = None
    # 键值合并更新;metadata 中某键的值为 None 表示删除该键(镜像 Kode §6.3)。
    # S2 实现依赖 model_fields_set 区分「未传」与「显式 None」。
