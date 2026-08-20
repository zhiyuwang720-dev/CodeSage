"""MCP 工具桥接测试(spec 12.1:test_mcp_tool.py)。

覆盖:McpTool 转换(命名/描述截断/input_schema/并发声明)、needs_permissions 恒 True、
调用转发+结果归一、资源全局单例注入、装配注入。
"""

import asyncio
from pathlib import Path

import pytest

from codesage.mcp import McpManager
from codesage.mcp.tool import ListMcpResourcesTool, McpTool, ReadMcpResourceTool, build_mcp_tools
from codesage.mcp.types import MCP_METHODS, ConfigScope, ScopedMcpServerConfig


class FakeTransport:
    """最小假传输:按 method 返回固定响应,记录调用。"""

    def __init__(self, name, *, tools=(), resources=(), prompts=(), caps=None):
        self.name = name
        self.tools = tools
        self.resources = resources
        self.prompts = prompts
        # 声明支持的能力(默认全支持;测试可覆盖以模拟不支持某能力的服务器)
        self.caps = caps if caps is not None else {"tools": {}, "resources": {}, "prompts": {}}
        self.sent: list[str] = []
        self.closed = False

    async def connect(self):
        pass

    def set_notification_handler(self, handler):
        self.notif_handler = handler

    async def send(self, msg):
        from codesage.mcp.jsonrpc import JsonRpcResponse

        self.sent.append(msg.method)
        if msg.method == MCP_METHODS.INITIALIZE:
            return JsonRpcResponse(id=msg.id, result={"capabilities": self.caps})
        if msg.method == MCP_METHODS.TOOLS_LIST:
            return JsonRpcResponse(id=msg.id, result={"tools": list(self.tools)})
        if msg.method == MCP_METHODS.RESOURCES_LIST:
            return JsonRpcResponse(id=msg.id, result={"resources": list(self.resources)})
        if msg.method == MCP_METHODS.PROMPTS_LIST:
            return JsonRpcResponse(id=msg.id, result={"prompts": list(self.prompts)})
        if msg.method == MCP_METHODS.TOOLS_CALL:
            return JsonRpcResponse(id=msg.id, result={"content": [{"type": "text", "text": str(msg.params.get("arguments", {}))}]})
        if msg.method == MCP_METHODS.RESOURCES_READ:
            return JsonRpcResponse(id=msg.id, result={"contents": [{"uri": msg.params["uri"], "text": "hello resource"}]})
        return JsonRpcResponse(id=msg.id, result={})

    async def close(self):
        self.closed = True


def make_manager(name="srv", **kw) -> tuple[McpManager, FakeTransport]:
    created: dict[str, FakeTransport] = {name: FakeTransport(name, **kw)}

    def factory(cfg):
        if cfg.name not in created:
            created[cfg.name] = FakeTransport(cfg.name, **kw)
        return created[cfg.name]

    mgr = McpManager(transport_factory=factory)
    return mgr, created[name]


def make_cfg(name: str) -> ScopedMcpServerConfig:
    return ScopedMcpServerConfig(name=name, scope=ConfigScope.LOCAL, command="echo")


@pytest.mark.asyncio
async def test_mcp_tool_conversion():
    """spec §7.1:命名 mcp__server__tool + 描述/参数透传 + 并发声明。"""
    mgr, fake = make_manager(tools=[{"name": "echo", "description": "回显", "inputSchema": {"type": "object"}}])
    await mgr.connect_server("srv", make_cfg("srv"))
    tool = McpTool(mgr, "srv", {"name": "echo", "description": "回显", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}})
    assert tool.name == "mcp__srv__echo"
    assert tool.description == "回显"
    assert tool.input_schema == {"type": "object"}
    assert tool.is_concurrency_safe is True  # readOnlyHint → 可并发


@pytest.mark.asyncio
async def test_mcp_tool_needs_permissions_always_true():
    """spec 裁决 3:MCP 工具恒需权限(服务器描述不可信)。"""
    mgr, _ = make_manager(tools=[{"name": "echo"}])
    await mgr.connect_server("srv", make_cfg("srv"))
    tool = McpTool(mgr, "srv", {"name": "echo", "description": ""})
    assert tool.needs_permissions({}) is True


@pytest.mark.asyncio
async def test_mcp_tool_description_truncated():
    """spec §7.1:超长描述截断防爆上下文。"""
    mgr, _ = make_manager(tools=[{"name": "echo"}])
    await mgr.connect_server("srv", make_cfg("srv"))
    long = "x" * 5000
    tool = McpTool(mgr, "srv", {"name": "echo", "description": long})
    assert len(tool.description) <= 2065
    assert tool.description.endswith("… [truncated]")


@pytest.mark.asyncio
async def test_mcp_tool_call_returns_result():
    """spec §7.4:调用转发参数并归一化结果为 ToolResult。"""
    from codesage.tools import ToolResult, ToolUseContext

    mgr, fake = make_manager(tools=[{"name": "echo"}])
    await mgr.connect_server("srv", make_cfg("srv"))
    tool = McpTool(mgr, "srv", {"name": "echo", "description": ""})
    ctx = ToolUseContext(cwd=Path("."))
    result = await tool.call({"text": "hi"}, ctx).__anext__()
    assert isinstance(result, ToolResult)
    assert result.content == "{'text': 'hi'}"
    assert fake.sent.count(MCP_METHODS.TOOLS_CALL) == 1


@pytest.mark.asyncio
async def test_mcp_tool_error_result():
    """spec §8:服务器返回 isError → 提取文本作为错误内容(模型自愈)。"""

    class ErrTransport(FakeTransport):
        async def send(self, msg):
            from codesage.mcp.jsonrpc import JsonRpcResponse

            if msg.method == MCP_METHODS.TOOLS_CALL:
                return JsonRpcResponse(id=msg.id, result={"isError": True, "content": [{"type": "text", "text": "boom error"}]})
            return await super().send(msg)

    mgr = McpManager(transport_factory=lambda cfg: ErrTransport(cfg.name))
    await mgr.connect_server("srv", make_cfg("srv"))
    tool = McpTool(mgr, "srv", {"name": "echo", "description": ""})
    from codesage.tools import ToolUseContext

    result = await tool.call({}, ToolUseContext(cwd=Path("."))).__anext__()
    assert result.is_error is True
    assert "boom error" in result.content


@pytest.mark.asyncio
async def test_build_mcp_tools_resources_global_singleton():
    """spec §7.2/§10.1:资源全局单例——多个服务器也只注入一份 List/Read。"""
    mgr = McpManager(
        configs={"a": make_cfg("a"), "b": make_cfg("b")},
        transport_factory=lambda cfg: FakeTransport(cfg.name, resources=[{"uri": "x"}]),
    )
    await mgr.connect_all()
    tools = await build_mcp_tools(mgr)
    names = [t.name for t in tools]
    assert "mcp__a__echo" not in names  # 没有工具定义
    assert names.count("ListMcpResourcesTool") == 1
    assert names.count("ReadMcpResourceTool") == 1


@pytest.mark.asyncio
async def test_build_mcp_tools_no_resources_no_singleton():
    """spec §10.1:无服务器支持 resources 时不注入资源工具。"""
    mgr = McpManager(
        configs={"a": make_cfg("a")},
        transport_factory=lambda cfg: FakeTransport(
            cfg.name, tools=[{"name": "echo"}], caps={"tools": {}},
        ),
    )
    await mgr.connect_all()
    tools = await build_mcp_tools(mgr)
    names = [t.name for t in tools]
    assert "mcp__a__echo" in names
    assert "ListMcpResourcesTool" not in names


@pytest.mark.asyncio
async def test_resource_tools():
    """spec §10.1:资源工具列出/读取资源。"""
    mgr, fake = make_manager(resources=[{"uri": "codesage://demo/version", "text": "0.1.0"}])
    await mgr.connect_server("srv", make_cfg("srv"))
    from codesage.tools import ToolUseContext

    ctx = ToolUseContext(cwd=Path("."))
    list_tool = ListMcpResourcesTool(mgr)
    listed = await list_tool.call({}, ctx).__anext__()
    assert "codesage://demo/version" in listed.content

    read_tool = ReadMcpResourceTool(mgr)
    read = await read_tool.call({"server": "srv", "uri": "codesage://demo/version"}, ctx).__anext__()
    assert "hello resource" in read.content