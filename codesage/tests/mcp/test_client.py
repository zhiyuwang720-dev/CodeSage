"""连接管理测试(spec 12.1 镜像清单:test_client.py)。

用 FakeTransport 隔离网络,覆盖:连接 memoize/失败降级/抓取缓存/listChanged 失效/
调用转发/禁用跳过/批处理容错。
"""

import asyncio

import pytest

from codesage.mcp.client import McpManager
from codesage.mcp.jsonrpc import JsonRpcResponse
from codesage.mcp.types import MCP_METHODS, ConfigScope, ScopedMcpServerConfig

def make_cfg(name: str, **kw) -> ScopedMcpServerConfig:
    return ScopedMcpServerConfig(name=name, scope=ConfigScope.LOCAL, command=kw.pop("command", "echo"), **kw)


class FakeTransport:
    """记录调用、按 method 返回固定响应的假传输。"""

    def __init__(self, name, *, fail_connect=False, tools=(), resources=(), prompts=()):
        self.name = name
        self.fail_connect = fail_connect
        self.tools = tools
        self.resources = resources
        self.prompts = prompts
        self.sent: list[str] = []  # 已发送的 method 序列
        self.notif_handler = None
        self.closed = False

    async def connect(self):
        if self.fail_connect:
            raise ConnectionError("boom")
        self.closed = False

    def set_notification_handler(self, handler):
        self.notif_handler = handler

    async def send(self, msg):
        self.sent.append(msg.method)
        if msg.method == MCP_METHODS.INITIALIZE:
            return JsonRpcResponse(id=msg.id, result={"capabilities": {"tools": {}, "resources": {}, "prompts": {}}, "serverInfo": {"name": self.name}})
        if msg.method == MCP_METHODS.TOOLS_LIST:
            return JsonRpcResponse(id=msg.id, result={"tools": list(self.tools)})
        if msg.method == MCP_METHODS.RESOURCES_LIST:
            return JsonRpcResponse(id=msg.id, result={"resources": list(self.resources)})
        if msg.method == MCP_METHODS.PROMPTS_LIST:
            return JsonRpcResponse(id=msg.id, result={"prompts": list(self.prompts)})
        if msg.method == MCP_METHODS.TOOLS_CALL:
            return JsonRpcResponse(id=msg.id, result={"content": [{"type": "text", "text": str(msg.params.get("arguments", {}))}]})
        return JsonRpcResponse(id=msg.id, result={})

    async def close(self):
        self.closed = True

    async def _emit(self, method: str):
        """模拟服务器推送通知。"""
        if self.notif_handler:
            from codesage.mcp.jsonrpc import JsonRpcNotification
            await self.notif_handler(JsonRpcNotification(method=method, params={}))


def make_manager(name="srv", **kw) -> tuple[McpManager, dict[str, FakeTransport]]:
    created: dict[str, FakeTransport] = {name: FakeTransport(name, **kw)}

    def factory(cfg):
        if cfg.name not in created:
            created[cfg.name] = FakeTransport(cfg.name, **kw)
        return created[cfg.name]

    mgr = McpManager(transport_factory=factory)
    return mgr, created


@pytest.mark.asyncio
async def test_connect_memoize_same_config():
    """spec §6.1:同配置只连接一次(第二次走缓存)。"""
    seen = {"n": 0}
    def factory(cfg):
        seen["n"] += 1
        return FakeTransport(cfg.name)
    mgr = McpManager(transport_factory=factory)
    cfg = make_cfg("srv")
    c1 = await mgr.connect_server("srv", cfg)
    c2 = await mgr.connect_server("srv", cfg)
    assert c1 is c2  # 缓存复用同一对象
    assert seen["n"] == 1


@pytest.mark.asyncio
async def test_connect_config_change_reconnects():
    """spec §6.1:配置变化(内容签名变)触发重连。"""
    seen = {"n": 0}
    def factory(cfg):
        seen["n"] += 1
        return FakeTransport(cfg.name)
    mgr = McpManager(transport_factory=factory)
    await mgr.connect_server("srv", make_cfg("srv", command="echo"))
    await mgr.connect_server("srv", make_cfg("srv", command="other"))  # 命令变了
    assert seen["n"] == 2


@pytest.mark.asyncio
async def test_connect_failed_degrade():
    """spec §6.1:连接抛错 → failed 状态带错误信息。"""
    mgr, _ = make_manager(fail_connect=True)
    conn = await mgr.connect_server("srv", make_cfg("srv"))
    assert conn.state.value == "failed"
    assert "boom" in conn.error


@pytest.mark.asyncio
async def test_fetch_tools_cached_until_invalidate():
    """spec §6.4:工具列表缓存;invalidate 后重拉。"""
    mgr, created = make_manager(tools=[{"name": "echo"}])
    fake = created["srv"]
    cfg = make_cfg("srv")
    await mgr.connect_server("srv", cfg)
    await mgr.fetch_tools("srv")
    tools = await mgr.fetch_tools("srv")
    assert len(tools) == 1
    assert fake.sent.count(MCP_METHODS.TOOLS_LIST) == 1  # 缓存命中,只发一次
    mgr.invalidate("srv")
    await mgr.fetch_tools("srv")
    assert fake.sent.count(MCP_METHODS.TOOLS_LIST) == 2  # 失效后重拉


@pytest.mark.asyncio
async def test_list_changed_notification_invalidates():
    """spec §6.4:*_list_changed 通知清缓存。"""
    mgr, created = make_manager(tools=[{"name": "echo"}])
    fake = created["srv"]
    await mgr.connect_server("srv", make_cfg("srv"))
    await mgr.fetch_tools("srv")
    await fake._emit(MCP_METHODS.NOTIFICATION_TOOLS_LIST_CHANGED)
    assert "srv" not in mgr._tools_cache  # 缓存已清
    await mgr.fetch_tools("srv")
    assert fake.sent.count(MCP_METHODS.TOOLS_LIST) == 2


@pytest.mark.asyncio
async def test_call_tool_forwards():
    """spec §7.4:call_tool 转发参数并返回 result。"""
    mgr, created = make_manager()
    fake = created["srv"]
    await mgr.connect_server("srv", make_cfg("srv"))
    result = await mgr.call_tool("srv", "echo", {"text": "hi"})
    assert result["content"][0]["text"] == "{'text': 'hi'}"
    assert fake.sent.count(MCP_METHODS.TOOLS_CALL) == 1


@pytest.mark.asyncio
async def test_call_tool_not_connected():
    """spec §7.4:未连接的服务器调用抛 ConnectionError。"""
    mgr, _ = make_manager()
    with pytest.raises(ConnectionError):
        await mgr.call_tool("srv", "echo", {})


@pytest.mark.asyncio
async def test_connect_all_degrades_failure():
    """spec §6.3:单服务器连接失败不中断其余。"""
    cfg_a = make_cfg("a", command="ok")
    cfg_b = make_cfg("b", command="ok")

    def factory(cfg):
        if cfg.name == "b":
            return FakeTransport(cfg.name, fail_connect=True)
        return FakeTransport(cfg.name)

    mgr = McpManager(configs={"a": cfg_a, "b": cfg_b}, transport_factory=factory)
    await mgr.connect_all()
    conn_a = mgr.get_connection("a")
    conn_b = mgr.get_connection("b")
    assert conn_a.state.value == "connected"
    assert conn_b.state.value == "failed"
    assert "boom" in conn_b.error


@pytest.mark.asyncio
async def test_fetch_all_concurrent():
    """spec §6.3:一个服务器并发抓取工具/资源/提示词。"""
    mgr, created = make_manager(
        tools=[{"name": "echo"}], resources=[{"uri": "x"}], prompts=[{"name": "greet"}],
    )
    fake = created["srv"]
    await mgr.connect_server("srv", make_cfg("srv"))
    tools, resources, prompts = await mgr.fetch_all("srv")
    assert tools[0]["name"] == "echo"
    assert resources[0]["uri"] == "x"
    assert prompts[0]["name"] == "greet"