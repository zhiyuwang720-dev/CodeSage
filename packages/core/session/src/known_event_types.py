"""已知事件词表:本构建能理解的全部事件类型。

DSH 中本文件由脚本生成(gen-persistence-catalog),汇总仓库内所有
SessionEventMap 成员 —— 事件词表通过 TypeScript 声明合并跨包生长。
Python 没有声明合并,这份词表以注册表形式承载同一概念:

- KNOWN_SESSION_EVENT_TYPES:本仓库理解的词表(照 DSH 生成物原样);
- extend_event_types:其他包注册自己事件的入口,模拟声明合并。

词表的语义边界(照 DSH):
持久化读路径拒绝解释词表外的事件类型,除非事件带 ignorable 标记
(见 SessionEvent.ignorable):一条包含未知类型的日志多半来自更新的
harness,静默跳过必需事件会重建出错误的会话。词表外的下游插件
事件不在本列表内 —— 注册面留给真正出现消费者时再开。
"""

from __future__ import annotations

__all__ = ["KNOWN_SESSION_EVENT_TYPES", "extend_event_types"]

#: 本构建理解的事件词表:读路径遇到词表外的必读事件必须拒绝。
KNOWN_SESSION_EVENT_TYPES: frozenset[str] = frozenset({
    "agent-preset/selected",
    "agent/inbox/spliced",
    "approval/asked",
    "approval/decided",
    "approval/policy",
    "assistant/chunk",
    "assistant/message",
    "command/done",
    "command/run",
    "compaction/end",
    "compaction/prune",
    "compaction/start",
    "compaction/summary",
    "feedback/record",
    "goal/change",
    "hook/invoked",
    "hook/result",
    "llm/retry",
    "llm/retry-started",
    "permission/preset",
    "plan/mode",
    "request/context",
    "request/header",
    "sandbox/mode",
    "schedule/change",
    "session/end-seed",
    "session/title",
    "session/title-llm-request",
    "step/end",
    "step/start",
    "subagent/descriptor",
    "team/member",
    "team/message/delivered",
    "team/message/queued",
    "team/task",
    "todo/write",
    "tool-workflow/agent-end",
    "tool-workflow/agent-start",
    "tool-workflow/run-end",
    "tool-workflow/run-start",
    "tool/call",
    "tool/code-dispatch",
    "tool/code-dispatch-start",
    "tool/result",
    "turn/end",
    "turn/start",
    "user/message",
    "web/deepseek-search-llm-request",
})


def extend_event_types(*types: str) -> None:
    """注册词表外的事件类型(声明合并的 Python 落点)。

    供其他包在安装时把自有的 SessionEventMap 扩展成员登记进词表,
    使持久化读路径可以解释它们。重复注册与空参是幂等的。
    """
    if not types:
        return
    global KNOWN_SESSION_EVENT_TYPES
    KNOWN_SESSION_EVENT_TYPES = KNOWN_SESSION_EVENT_TYPES | frozenset(types)
