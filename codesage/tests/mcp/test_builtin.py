"""内建 echo 服务器单测(spec 12.1:test_builtin.py 之一)。

直接调用 handle() 验证协议行为;端到端子进程路径见 test_transports.py。
"""

import pytest

from codesage.mcp.builtin.echo_server import handle
from codesage.mcp.jsonrpc import decode


def _run_handle(method, msg_id=1, params=None):
    """捕获 handle 的输出(写 stdout),返回解析后的响应字典。"""
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        handle(method, msg_id, params or {})
    return decode(buf.getvalue())


def test_echo_server_initialize():
    resp = _run_handle("initialize", 1)
    assert resp.id == 1
    assert resp.result["serverInfo"]["name"] == "codesage-echo"
    assert resp.result["protocolVersion"] == "2025-03-26"


def test_echo_server_tools_list():
    resp = _run_handle("tools/list", 1)
    names = [t["name"] for t in resp.result["tools"]]
    assert names == ["echo", "add"]


def test_echo_server_tools_call_echo():
    resp = _run_handle("tools/call", 1, {"name": "echo", "arguments": {"text": "hello"}})
    assert resp.result["content"][0]["text"] == "hello"


def test_echo_server_tools_call_add():
    resp = _run_handle("tools/call", 1, {"name": "add", "arguments": {"a": 2, "b": 3}})
    assert resp.result["content"][0]["text"] == "5"


def test_echo_server_unknown_tool_error():
    resp = _run_handle("tools/call", 1, {"name": "nope"})
    assert resp.error is not None
    assert resp.error.code == -32602


def test_echo_server_resources():
    resp = _run_handle("resources/list", 1)
    assert resp.result["resources"][0]["uri"] == "codesage://demo/version"
    resp = _run_handle("resources/read", 2, {"uri": "codesage://demo/version"})
    assert resp.result["contents"][0]["text"] == "0.1.0"


def test_echo_server_prompts():
    resp = _run_handle("prompts/list", 1)
    assert resp.result["prompts"][0]["name"] == "greet"
    resp = _run_handle("prompts/get", 2, {"name": "greet", "arguments": {"name": "Sage"}})
    assert "你好 Sage" in resp.result["messages"][0]["content"]["text"]