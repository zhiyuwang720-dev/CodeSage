"""tasks 任务系统(阶段 11):契约层 + 只读图验证 + 持久化存储。

S1 交付:types(数据模型)+ graph(只读四类验证,纯函数,无 IO)。
S2 交付:storage(持久化 TaskStore,一任务一文件 + 双层锁 + 高水位 ID)。
S3 起接四工具。
"""

from .graph import MissingTaskDependency, TaskGraphValidation, validate_task_graph
from .storage import TaskStore, TaskStoreError, get_task_store, resolve_task_list_id
from .types import Task, TaskStatus, TaskSummary, TaskUpdate

__all__ = [
    "MissingTaskDependency",
    "Task",
    "TaskGraphValidation",
    "TaskStatus",
    "TaskStore",
    "TaskStoreError",
    "TaskSummary",
    "TaskUpdate",
    "get_task_store",
    "resolve_task_list_id",
    "validate_task_graph",
]
