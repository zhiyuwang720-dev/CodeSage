"""已知事件词表测试:核心词表覆盖、声明合并注册入口。

照 DSH 生成词表的语义:词表外的类型读路径拒绝(除非 ignorable),
extend_event_types 是其他包注册自有事件的入口,须幂等。
"""

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]  # 包目录 core/session
sys.path.insert(0, str(_CORE))

import core.session.src.known_event_types as kt  # noqa: E402


def test_vocabulary_contains_core_event_types():
    # 本构建的核心事件词表(13 种)必须全部在已知词表中
    core = {
        "turn/start",
        "turn/end",
        "step/start",
        "step/end",
        "user/message",
        "assistant/chunk",
        "assistant/message",
        "tool/call",
        "tool/result",
        "todo/write",
        "request/header",
        "request/context",
        "session/end-seed",
    }
    assert core <= kt.KNOWN_SESSION_EVENT_TYPES


def test_vocabulary_has_cross_package_types():
    # DSH 词表由声明合并跨包生长:其他包的事件也应在其中
    for type_ in (
        "approval/asked",
        "approval/decided",
        "compaction/start",
        "compaction/end",
        "compaction/summary",
        "hook/invoked",
        "hook/result",
        "llm/retry",
        "permission/preset",
        "plan/mode",
        "sandbox/mode",
        "session/title",
        "subagent/descriptor",
        "todo/write",
        "tool-workflow/agent-start",
    ):
        assert type_ in kt.KNOWN_SESSION_EVENT_TYPES


def test_extend_event_types_registers_new_types():
    # 经模块引用读取,确保看到的是扩展后的全局
    assert "custom/thing" not in kt.KNOWN_SESSION_EVENT_TYPES
    kt.extend_event_types("custom/thing", "custom/other")
    assert "custom/thing" in kt.KNOWN_SESSION_EVENT_TYPES
    assert "custom/other" in kt.KNOWN_SESSION_EVENT_TYPES


def test_extend_event_types_is_idempotent():
    before = kt.KNOWN_SESSION_EVENT_TYPES
    kt.extend_event_types("custom/thing")
    kt.extend_event_types("custom/thing", "custom/thing")
    assert kt.KNOWN_SESSION_EVENT_TYPES == before
    # 空参不破坏任何东西
    kt.extend_event_types()
    assert kt.KNOWN_SESSION_EVENT_TYPES == before
