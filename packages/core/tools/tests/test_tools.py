"""tools 契约面表单测:错误类/常量/词表注册/空表语义/辅助函数。"""

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]  # 仓库根
_PACKAGES = Path(__file__).resolve().parents[3]  # packages/
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from cordis import Context  # noqa: E402

from core.session import KNOWN_SESSION_EVENT_TYPES  # noqa: E402
from core.tools import (  # noqa: E402
    TOOL_ABORTED,
    TOOL_ABORTED_BEFORE_DISPATCH,
    TOOL_RUNTIME_SCHEDULER,
    ToolNotFoundError,
    ToolOutputError,
    ToolRuntime,
    error_message,
    failure_message_from_content,
    tool_error_result,
)
from core.tools.src.types import CodeDispatchEventData, CodeDispatchStartEventData  # noqa: E402

from llm.llm.src.error_chain import HarnessError  # noqa: E402


# ---- 错误类 ----


def test_tool_not_found_error_routes_by_code():
    err = ToolNotFoundError("ghost")
    assert isinstance(err, HarnessError)
    assert err.code == "UNKNOWN_TOOL"
    assert err.name == "ToolNotFoundError"
    assert "ghost" in str(err)
    # 带替代路径时消息给出可达工具名
    routed = ToolNotFoundError("bash", "only `run_code` is callable directly")
    assert routed.code == "UNKNOWN_TOOL"
    assert "run_code" in str(routed)


def test_tool_output_error_carries_violations():
    err = ToolOutputError("bash", ["value is not lossless JSON", "render failed"])
    assert err.code == "INVALID_TOOL_OUTPUT"
    assert err.name == "ToolOutputError"
    assert err.violations == ["value is not lossless JSON", "render failed"]
    assert "bash" in str(err)


# ---- 常量 ----


def test_abort_codes():
    assert TOOL_ABORTED == "ABORTED"
    assert TOOL_ABORTED_BEFORE_DISPATCH == "ABORTED_BEFORE_DISPATCH"
    assert TOOL_RUNTIME_SCHEDULER is not None  # 哨兵存在
    assert TOOL_RUNTIME_SCHEDULER is not TOOL_ABORTED  # 符号 ≠ 字符串


# ---- 事件词表注册(types.py 导入副作用) ----


def test_code_dispatch_event_types_registered():
    assert "tool/code-dispatch-start" in KNOWN_SESSION_EVENT_TYPES
    assert "tool/code-dispatch" in KNOWN_SESSION_EVENT_TYPES


def test_event_data_shapes():
    start: CodeDispatchStartEventData = {
        "rootCallId": "root-1",
        "parentCallId": "parent-1",
        "subCallId": "parent-1:code:0",
        "name": "bash",
        "arguments": {"cmd": "ls"},
    }
    settled: CodeDispatchEventData = {
        **start,
        "isError": False,
        "content": [{"type": "text", "text": "ok"}],
    }
    assert settled["subCallId"] == "parent-1:code:0"
    assert settled["isError"] is False


# ---- 空注册表 fail-closed 语义 ----


@pytest.fixture
def runtime():
    ctx = Context()
    return ToolRuntime(ctx)


def test_execution_mode_empty_registry_exclusive(runtime):
    exec_ = {
        "callId": "c1",
        "name": "bash",
        "arguments": {"cmd": "ls"},
        "agent": None,
        "parent": None,
        "signal": None,
    }
    assert runtime.executionMode(exec_) == {"kind": "exclusive"}


def test_scheduler_unknown_tool(runtime):
    """空表下任何派发都是 UNKNOWN_TOOL 失败,流程可运行不崩溃。"""
    exec_ = {
        "callId": "c1",
        "name": "bash",
        "arguments": {},
        "agent": None,
        "parent": None,
        "signal": None,
    }
    prepared = asyncio.run(runtime[TOOL_RUNTIME_SCHEDULER].prepare(exec_))
    assert prepared["kind"] == "final-result"
    result = prepared["result"]
    assert result["isError"] is True
    assert result["error"]["info"]["code"] == "UNKNOWN_TOOL"
    assert "unknown tool" in result["content"][0]["text"]


def test_scheduler_finalize_finish_pass_through(runtime):
    result = tool_error_result(ToolNotFoundError("x"))
    assert asyncio.run(runtime[TOOL_RUNTIME_SCHEDULER].finalize(None, result)) is result
    assert runtime[TOOL_RUNTIME_SCHEDULER].finish(None, result) is result


def test_scheduler_slot_misses_other_keys(runtime):
    with pytest.raises(KeyError):
        runtime[object()]


def test_max_parallel_sub_calls_validation():
    assert ToolRuntime(Context(), {"maxParallelSubCalls": 1}).maxParallelSubCalls == 1
    with pytest.raises(TypeError, match="positive integer"):
        ToolRuntime(Context(), {"maxParallelSubCalls": 0})
    assert ToolRuntime(Context()).maxParallelSubCalls == 10  # 默认


# ---- 辅助函数 ----


def test_error_message_normalization():
    assert error_message(RuntimeError("boom")) == "boom"
    assert error_message({"message": "denied"}) == "denied"
    assert error_message(42) == "42"


def test_failure_message_from_content():
    blocks = [{"type": "text", "text": "no"}, {"type": "thinking", "text": "hidden"}]
    assert failure_message_from_content(blocks) == "no\n[thinking content]"
    assert failure_message_from_content([]) == "tool result blocked by post-execute policy"


def test_tool_error_result_shapes():
    result = tool_error_result(ToolNotFoundError("ghost"))
    assert result["isError"] is True
    assert result["error"]["message"] == 'unknown tool "ghost"'
    assert result["error"]["info"] == {"name": "ToolNotFoundError", "code": "UNKNOWN_TOOL"}
    assert result["content"][0]["type"] == "text"
    # 非 HarnessError 不带 info
    plain = tool_error_result(RuntimeError("boom"))
    assert plain["error"]["info"] is None
