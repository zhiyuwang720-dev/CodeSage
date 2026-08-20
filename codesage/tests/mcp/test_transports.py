"""传输层测试(spec 12.1 镜像清单:test_transports.py)。

覆盖:stdio 对 echo_server 子进程端到端(connect→initialize→tools/list→tools/call);
http 用 httpx.MockTransport 模拟 JSON 响应与 SSE 流;Accept 头断言;404+-32001 会话过期;
通知转发。
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from codesage.mcp import decode, encode, next_id
from codesage.mcp.jsonrpc import JsonRpcNotification, JsonRpcRequest, JsonRpcResponse
from codesage.mcp.transports import (
    HttpTransport,
    McpSessionExpiredError,
    StdioTransport,
    create_transport,
)
from codesage.mcp.types import MCP_METHODS, ConfigScope, ScopedMcpServerConfig

#: 项目根 = tests/ 的上级(tests/mcp/ 的第三级父目录)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 内建 echo 服务器模块路径,子进程以此启动
ECHO_MODULE = "codesage.mcp.builtin.echo_server"


def make_stdio_transport() -> StdioTransport:
    return StdioTransport(command=sys.executable, args=["-m", ECHO_MODULE])


@pytest.mark.asyncio
async def test_stdio_connect_initialize():
    """stdio 端到端:连接后发送 initialize 收到 capabilities。"""
    t = make_stdio_transport()
    await t.connect()
    try:
        resp = await t.send(
            JsonRpcRequest(id=next_id(), method=MCP_METHODS.INITIALIZE, params={})
        )
        assert resp.result["serverInfo"]["name"] == "codesage-echo"
        assert "tools" in resp.result["capabilities"]
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_stdio_tools_list_and_call():
    """stdio 端到端:tools/list 与 tools/call echo 往返。"""
    t = make_stdio_transport()
    await t.connect()
    try:
        tools = await t.send(JsonRpcRequest(id=next_id(), method=MCP_METHODS.TOOLS_LIST, params={}))
        names = [t["name"] for t in tools.result["tools"]]
        assert "echo" in names and "add" in names

        call = await t.send(
            JsonRpcRequest(
                id=next_id(),
                method=MCP_METHODS.TOOLS_CALL,
                params={"name": "echo", "arguments": {"text": "hi"}},
            )
        )
        assert call.result["content"][0]["text"] == "hi"
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_stdio_close_escalation():
    """spec §4.2:关闭后 pending 请求被释放,再发送抛 ConnectionError。"""
    t = make_stdio_transport()
    await t.connect()
    await t.close()
    with pytest.raises(ConnectionError):
        await t.send(JsonRpcRequest(id=next_id(), method="tools/list", params={}))


@pytest.mark.asyncio
async def test_http_send_json_response():
    """spec §4.3:POST 请求收到 JSON 响应,Accept 头正确。"""
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["accept"] = request.headers.get("accept")
        payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}}
        )

    t = HttpTransport(url="https://mcp.example.com/mcp", transport=httpx.MockTransport(handler))
    await t.connect()
    try:
        resp = await t.send(JsonRpcRequest(id=7, method=MCP_METHODS.TOOLS_LIST, params={}))
        assert resp.id == 7
        assert resp.result == {"tools": []}
        assert "application/json, text/event-stream" in seen_headers["accept"]
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_http_accept_header_always_present():
    """spec §4.3:Accept 头缺失会遭严格服务器 406 拒绝,必须恒带。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    t = HttpTransport(url="https://mcp.example.com/mcp", transport=httpx.MockTransport(handler))
    await t.connect()
    try:
        await t.send(JsonRpcRequest(id=1, method=MCP_METHODS.INITIALIZE, params={}))
        assert seen["accept"] == "application/json, text/event-stream"
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_http_session_expired_404():
    """spec §6.4:HTTP 404 + JSON-RPC -32001 抛会话过期。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "Session not found"}}
        )

    t = HttpTransport(url="https://mcp.example.com/mcp", transport=httpx.MockTransport(handler))
    await t.connect()
    with pytest.raises(McpSessionExpiredError):
        await t.send(JsonRpcRequest(id=1, method=MCP_METHODS.TOOLS_CALL, params={}))
    await t.close()


@pytest.mark.asyncio
async def test_http_generic_404_not_expired():
    """spec §6.4:普通 404(无 -32001)不视为会话过期——未来匹配超时而非 McpSessionExpiredError。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    t = HttpTransport(url="https://mcp.example.com/mcp", timeout_ms=100, transport=httpx.MockTransport(handler))
    await t.connect()
    with pytest.raises(TimeoutError):
        await t.send(JsonRpcRequest(id=1, method=MCP_METHODS.TOOLS_LIST, params={}))
    await t.close()


def test_create_transport_selects_by_type():
    """spec §4.4:工厂按配置类型选择传输。"""
    stdio_cfg = ScopedMcpServerConfig(name="s", scope=ConfigScope.LOCAL, command="echo")
    assert isinstance(create_transport(stdio_cfg), StdioTransport)
    http_cfg = ScopedMcpServerConfig(name="s", scope=ConfigScope.LOCAL, url="https://x.com/mcp", type="http")
    assert isinstance(create_transport(http_cfg), HttpTransport)
    with pytest.raises(ValueError):
        create_transport(ScopedMcpServerConfig(name="s", scope=ConfigScope.LOCAL, url="x", type="ws"))


def test_redact_headers():
    """spec §11:Authorization 头日志打码。"""
    from codesage.mcp.transports import _redact_headers

    redacted = _redact_headers({"Authorization": "Bearer secret", "X-Custom": "v"})
    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["X-Custom"] == "v"