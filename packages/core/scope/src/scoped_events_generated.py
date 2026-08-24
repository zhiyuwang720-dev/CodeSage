"""作用域筛选事件的主题解析表

每个作用域筛选事件配一个「主题解析器」:从事件参数里取出路由键
(即事件的 subject)。解析器为 None 表示该事件的 payload 无法暴露外部
路由键 —— 检查岗只查载体存在性,不比对其键。哨兵表示该事件不是
作用域筛选事件,检查岗直接放行。
"""

from __future__ import annotations

from typing import Any, Callable

#: 非作用域事件的哨兵
_NOT_SCOPED = object()

_SCOPED_SUBJECT_RESOLVERS: dict[str, Callable[[list], Any] | None] = {
    "agent/created": lambda args: args[0]["agent"],
    "agent/disposed": lambda args: args[0]["agent"],
    "agent/error": lambda args: args[0]["agent"],
    "agent/inbox/claimed": lambda args: args[0]["agent"],
    "agent/inbox/discarded": lambda args: args[0]["agent"],
    "agent/inbox/inserted": lambda args: args[0]["agent"],
    "agent/pre-step": lambda args: args[0]["agent"],
    "agent/request": lambda args: args[0]["agent"],
    "agent/request-error": lambda args: args[0]["agent"],
    "agent/session-start": lambda args: args[0]["agent"],
    "agent/status": lambda args: args[0]["agent"],
    "agent/turn-stopping": lambda args: args[0]["agent"],
    "approval/request": lambda args: args[0]["agent"],
    "goal/changed": lambda args: args[0]["agent"],
    "session/created": None,
    "session/disposed": None,
    "session/event": None,
    "session/flush": None,
    "subagent/end": None,
    "subagent/start": None,
    "system-prompt/assemble": lambda args: args[1]["scope"],
    "tools/code-dispatch-log": lambda args: args[0]["agent"],
    "tools/execute": lambda args: args[0]["agent"],
    "tools/post-execute": lambda args: args[0]["agent"],
    "tools/pre-execute": lambda args: args[0]["agent"],
    "tools/result": lambda args: args[0]["agent"],
}


def scoped_subject_resolver_for(event: str):
    """按事件名取主题解析器;None 表示仅查载体存在性;哨兵表示非作用域事件。"""
    if event not in _SCOPED_SUBJECT_RESOLVERS:
        return _NOT_SCOPED
    return _SCOPED_SUBJECT_RESOLVERS[event]
