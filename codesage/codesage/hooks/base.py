"""Hook execution contract (phase 09): executor protocol and result shape.

S3 (command), S4 (http), S10 (prompt) implement HookExecutor; the S5
HookManager consumes HookResult and applies §4.6 fail-closed semantics
(JSON parse failure / timeout / spawn failure → deny on PreToolUse).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from .types import HookInput


@dataclass(slots=True)
class HookResult:
    """单个钩子的原始执行结果(未解析,§4.3)。

    超时 / spawn 失败由执行体抛异常(不构造 HookResult),HookManager 捕获后按
    §4.6 表 fail-closed;exit 1 与 exit 2 的语义区分在 §4.3 退出码表。
    """

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0


class HookExecutor(Protocol):
    """执行一个钩子,返回原始结果(§4.1-§4.2 契约由实现承担)。"""

    async def run(self, input_json: str, *, timeout: float) -> HookResult:
        """执行一次钩子调用。input_json 为 HookInput 的惰性序列化结果(§4.10.4)。"""


class HookManagerProtocol(Protocol):
    """事件分发器协议(§4.10 执行引擎管线)。具体实现在 S5(registry.py 的 HookManager)。"""

    def has_hooks_for_event(self, event: str) -> bool:
        """快速存在性检查(§4.10.1):索引空 → 零路径,不进管线。"""

    async def dispatch(
        self, event: str, *, input: HookInput, abort_event: asyncio.Event | None = None
    ) -> Any:
        """执行一次事件的完整管线(匹配 → 去重 → 顺序执行 → 聚合)。

        返回聚合结果(§4.10.6 逐事件消费总表);具体返回类型由 S5 定义。
        abort_event 置位时跳过剩余钩子,不产生决策(§6.3)。
        """

    async def notify(
        self,
        notification_type: str,
        message: str,
        *,
        title: str | None = None,
        **data: Any,
    ) -> None:
        """通知事件(§2.5):全事件 fail-open,默认超时 10s,不参与决策。

        matcher 取 notification_type;通知不产生权限审计事件(§9.2 红线)。
        """
