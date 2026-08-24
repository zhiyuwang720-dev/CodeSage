"""请求头工具测试:规范化、相等、折叠。

照 DSH request-header.spec.ts 的核心断言面:空字段归一、config
缺省感知相等、最后快照生效。
"""

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]  # 包目录 core/session
sys.path.insert(0, str(_CORE))

from core.session.src.request_header import (  # noqa: E402
    canonical_header,
    fold_request_header,
    header_equals,
)


def _header(**overrides):
    base = {"config": {"provider": "deepseek", "model": "m"}, "system": "sys", "tools": []}
    base.update(overrides)
    return base


def test_canonical_removes_empty_system_and_tools():
    h = canonical_header({"config": {"provider": "p"}, "system": "", "tools": []})
    assert h == {"config": {"provider": "p"}}
    # 非空保留
    h2 = canonical_header({"config": {"provider": "p"}, "system": "sys", "tools": [{"name": "t"}]})
    assert h2["system"] == "sys" and h2["tools"] == [{"name": "t"}]


def test_canonical_adapter_defaults_only_when_true():
    h = canonical_header({"config": {"provider": "p"}, "adapterDefaults": {"reasoningEffort": False, "maxTokens": False}})
    assert "adapterDefaults" not in h
    h2 = canonical_header({"config": {"provider": "p"}, "adapterDefaults": {"reasoningEffort": True, "maxTokens": False}})
    assert h2["adapterDefaults"] == {"reasoningEffort": True, "maxTokens": False}


def test_header_equals_config():
    assert header_equals(_header(), _header())
    assert header_equals(
        {"config": {"provider": "p", "stop": ["a"]}},
        {"config": {"provider": "p", "stop": ["a"]}},
    )
    # 缺省感知:未提供 == 显式 None
    assert header_equals({"config": {"provider": "p"}}, {"config": {"provider": "p", "temperature": None}})
    assert not header_equals({"config": {"provider": "p"}}, {"config": {"provider": "q"}})
    # stop 按序比较
    assert not header_equals(
        {"config": {"provider": "p", "stop": ["a", "b"]}},
        {"config": {"provider": "p", "stop": ["b", "a"]}},
    )


def test_header_equals_tools_and_adapter_defaults():
    a = _header(tools=[{"name": "t1", "args": {}}])
    b = _header(tools=[{"name": "t1", "args": {}}])
    assert header_equals(a, b)
    c = _header(tools=[{"name": "t2", "args": {}}])
    assert not header_equals(a, c)
    assert not header_equals(
        {"config": {"provider": "p"}, "adapterDefaults": {"maxTokens": True}},
        {"config": {"provider": "p"}, "adapterDefaults": {"maxTokens": False}},
    )


def test_fold_takes_last_header():
    events = [
        {"type": "request/header", "seq": 0, "time": 1, "data": {"header": {"config": {"provider": "p"}, "system": "v1"}}},
        {"type": "turn/start", "seq": 1, "time": 1, "data": {"turn": 1}},  # 非头事件跳过
        {"type": "request/header", "seq": 2, "time": 1, "data": {"header": {"config": {"provider": "p"}, "system": "v2"}}},
    ]
    result = fold_request_header(events)
    assert result == {"config": {"provider": "p"}, "system": "v2"}
    # 无头事件 → None
    assert fold_request_header([{"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}}]) is None


def test_fold_continues_from_state():
    state = canonical_header({"config": {"provider": "p"}, "system": "v1"})
    events = [
        {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}},
        {"type": "request/header", "seq": 1, "time": 1, "data": {"header": {"config": {"provider": "p"}, "system": "v2"}}},
    ]
    assert fold_request_header(events, state) == {"config": {"provider": "p"}, "system": "v2"}
