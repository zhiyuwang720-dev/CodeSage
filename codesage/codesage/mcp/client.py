"""MCP 连接管理(spec §6):连接单例、握手、状态机、批处理、缓存失效、重连。

负责把配置变成可用的连接:连接每个服务器(stdio/http),握手读 capabilities,
抓取工具/资源/提示词,并维护缓存失效(listChanged 通知 / 掉线 / 会话过期)与重连。
工具到 Tool 的桥接在 tool.py(S5)完成。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any, Callable

from .config import get_all_mcp_configs, is_mcp_server_disabled
from .jsonrpc import JsonRpcNotification, JsonRpcRequest, next_id
from .transports import BaseMcpTransport, McpSessionExpiredError, create_transport
from .types import MCP_METHODS, McpConnection, McpConnectionState, ScopedMcpServerConfig

logger = logging.getLogger(__name__)

#: 连接超时默认(毫秒),可用环境变量覆盖(spec §6.1)
DEFAULT_CONNECT_TIMEOUT_MS = 30_000

#: 本地(stdio)服务器并发连接数(起子进程慢,降并发;spec §6.3)
LOCAL_CONCURRENCY = 3
#: 远程(http)服务器并发连接数(纯网络,可高并发)
REMOTE_CONCURRENCY = 20

#: 终结性连接错误片段(spec §6.4:连续 3 次主动重连)
TERMINAL_ERROR_MARKERS = (
    "ECONNRESET", "ETIMEDOUT", "EPIPE", "EHOSTUNREACH", "ECONNREFUSED", "terminated",
)

#: 会话过期信号:HTTP 404 + JSON-RPC -32001(由 transport 抛 McpSessionExpiredError)


def get_server_cache_key(name: str, config: ScopedMcpServerConfig) -> str:
    """连接缓存键 = 名字 + 配置内容(scope 不参与;配置变则重连,spec §6.1)。"""
    return f"{name}|{config.signature() or config.model_dump_json(sort_keys=True)}"


class McpManager:
    """全部 MCP 服务器的连接与工具/资源/提示词管理器(spec §6)。

    进程内单例(装配层构建一个实例)。连接是瞬态:重启重新连接;本类不持久化任何东西。
    """

    def __init__(
        self,
        configs: dict[str, ScopedMcpServerConfig] | None = None,
        transport_factory: Callable[[ScopedMcpServerConfig], BaseMcpTransport] | None = None,
    ) -> None:
        #: 传输工厂(测试注入 FakeTransport;默认按配置类型创建)
        self._transport_factory = transport_factory or create_transport
        #: 名字 -> 连接对象(最近一次连接结果;掉线/重连会替换)
        self._connections: dict[str, McpConnection] = {}
        #: 连接缓存(配置键 -> 连接),保证同配置只连一次(类似 CC memoize)
        self._connect_cache: dict[str, McpConnection] = {}
        #: 名字 -> 工具列表缓存(listChanged/掉线时清空)
        self._tools_cache: dict[str, list[dict]] = {}
        self._resources_cache: dict[str, list[dict]] = {}
        self._commands_cache: dict[str, list[dict]] = {}
        #: 通知处理器(服务器推送 listChanged 等)
        self._notification_handler: Callable[[str, JsonRpcNotification], Any] | None = None
        self._configs = configs or {}

    # ---- 连接 ----

    def set_notification_handler(self, handler: Callable[[str, JsonRpcNotification], Any] | None) -> None:
        self._notification_handler = handler

    async def connect_server(self, name: str, config: ScopedMcpServerConfig) -> McpConnection:
        """连接单个服务器(带缓存:同配置只连一次;配置变/失效后重连)。"""
        key = get_server_cache_key(name, config)
        cached = self._connect_cache.get(key)
        if cached is not None and cached.state in (
            McpConnectionState.CONNECTED,
            McpConnectionState.PENDING,
        ):
            return cached

        conn = McpConnection(name=name, state=McpConnectionState.PENDING, config=config)
        self._connect_cache[key] = conn
        self._connections[name] = conn
        try:
            transport = self._transport_factory(config)
            conn.transport = transport
            transport.set_notification_handler(
                lambda msg: self._on_notification(name, msg)
            )
            await transport.connect()
            await self._handshake(conn)
            conn.state = McpConnectionState.CONNECTED
        except McpSessionExpiredError as e:
            conn.state = McpConnectionState.FAILED
            conn.error = str(e)
        except Exception as e:  # noqa: BLE001  # 连接失败降级为 failed,不中断其他服务器
            conn.state = McpConnectionState.FAILED
            conn.error = str(e)
            await self._close_transport(conn)
        return conn

    async def connect_all(self, timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS) -> list[McpConnection]:
        """批量连接全部配置(spec §6.3):本地与远程分组并发。失败降级不中断。"""
        configs = self._configs or get_all_mcp_configs()
        local: list[tuple[str, ScopedMcpServerConfig]] = []
        remote: list[tuple[str, ScopedMcpServerConfig]] = []
        for name, cfg in configs.items():
            if is_mcp_server_disabled(name):
                self._connections[name] = McpConnection(
                    name=name, state=McpConnectionState.DISABLED, config=cfg
                )
            elif cfg.type in (None, "stdio"):
                local.append((name, cfg))
            else:
                remote.append((name, cfg))

        async def _worker(pair: tuple[str, ScopedMcpServerConfig]) -> None:
            name, cfg = pair
            async with asyncio.timeout(timeout_ms / 1000):
                await self.connect_server(name, cfg)

        await asyncio.gather(
            self._batched(local, LOCAL_CONCURRENCY, _worker),
            self._batched(remote, REMOTE_CONCURRENCY, _worker),
        )
        return list(self._connections.values())

    @staticmethod
    async def _batched(items, concurrency, worker) -> None:
        """固定并发批处理(空槽即释放,单慢服务器不阻塞整批;spec §6.3)。"""
        sem = asyncio.Semaphore(concurrency)

        async def _run(item):
            async with sem:
                await worker(item)

        await asyncio.gather(*[_run(i) for i in items])

    async def _handshake(self, conn: McpConnection) -> None:
        """握手:initialize 读 capabilities/server_info/instructions(spec §6.2)。"""
        transport = conn.transport
        if transport is None:
            raise ConnectionError("transport is None")
        resp = await transport.send(
            JsonRpcRequest(id=next_id(), method=MCP_METHODS.INITIALIZE, params={
                "protocolVersion": "2025-03-26",
                "capabilities": {"roots": {}, "elicitation": {}},
                "clientInfo": {"name": "codesage", "version": "0.1.0"},
            })
        )
        if resp.error:
            raise ConnectionError(f"initialize failed: {resp.error.message}")
        result = resp.result or {}
        conn.capabilities = result.get("capabilities") or {}
        conn.server_info = result.get("serverInfo")
        conn.instructions = result.get("instructions") or None
        if conn.instructions and len(conn.instructions) > 2048:
            conn.instructions = conn.instructions[:2048] + "… [truncated]"

    async def disconnect(self, name: str) -> None:
        """断开并清理:关传输、清缓存(spec §6.5)。"""
        conn = self._connections.get(name)
        if conn:
            await self._close_transport(conn)
        self._connect_cache.pop(get_server_cache_key(name, conn.config), None) if conn else None
        self.invalidate(name)
        self._connections.pop(name, None)

    def invalidate(self, name: str) -> None:
        """失效该服务器的工具/资源/提示词缓存(listChanged 通知 / 掉线时调用)。"""
        self._tools_cache.pop(name, None)
        self._resources_cache.pop(name, None)
        self._commands_cache.pop(name, None)

    def get_connection(self, name: str) -> McpConnection | None:
        return self._connections.get(name)

    def connections(self) -> list[McpConnection]:
        return list(self._connections.values())

    def tools_for(self, name: str) -> list[dict]:
        return self._tools_cache.get(name, [])

    def all_tools(self) -> list[dict]:
        out: list[dict] = []
        for tools in self._tools_cache.values():
            out.extend(tools)
        return out

    def all_connections(self) -> list[McpConnection]:
        return list(self._connections.values())

    # ---- 抓取 ----

    async def fetch_tools(self, name: str) -> list[dict]:
        """拉取服务器工具列表(缓存,listChanged 通知/掉线时失效重拉)。"""
        conn = self._connections.get(name)
        if conn is None or conn.state != McpConnectionState.CONNECTED:
            return []
        cached = self._tools_cache.get(name)
        if cached is not None:
            return cached
        if "tools" not in conn.capabilities:
            return []
        resp = await conn.transport.send(
            JsonRpcRequest(id=next_id(), method=MCP_METHODS.TOOLS_LIST, params={})
        )
        tools = (resp.result or {}).get("tools", []) if not resp.error else []
        self._tools_cache[name] = tools
        return tools

    async def fetch_resources(self, name: str) -> list[dict]:
        """拉取资源列表(缓存;spec §10.1)。"""
        conn = self._connections.get(name)
        if conn is None or conn.state != McpConnectionState.CONNECTED:
            return []
        cached = self._resources_cache.get(name)
        if cached is not None:
            return cached
        if "resources" not in conn.capabilities:
            return []
        resp = await conn.transport.send(
            JsonRpcRequest(id=next_id(), method=MCP_METHODS.RESOURCES_LIST, params={})
        )
        resources = (resp.result or {}).get("resources", []) if not resp.error else []
        self._resources_cache[name] = resources
        return resources

    async def fetch_prompts(self, name: str) -> list[dict]:
        """拉取提示词列表(缓存;spec §10.2)。"""
        conn = self._connections.get(name)
        if conn is None or conn.state != McpConnectionState.CONNECTED:
            return []
        cached = self._commands_cache.get(name)
        if cached is not None:
            return cached
        if "prompts" not in conn.capabilities:
            return []
        resp = await conn.transport.send(
            JsonRpcRequest(id=next_id(), method=MCP_METHODS.PROMPTS_LIST, params={})
        )
        prompts = (resp.result or {}).get("prompts", []) if not resp.error else []
        self._commands_cache[name] = prompts
        return prompts

    async def fetch_all(self, name: str) -> tuple[list[dict], list[dict], list[dict]]:
        """并发抓取一个服务器的工具/资源/提示词(spec §6.3)。"""
        tools, resources, prompts = await asyncio.gather(
            self.fetch_tools(name), self.fetch_resources(name), self.fetch_prompts(name)
        )
        return tools, resources, prompts

    # ---- 调用 ----

    async def call_tool(
        self,
        name: str,
        tool_name: str,
        arguments: dict,
        *,
        timeout_ms: int = 300_000,
        on_progress: Callable[[dict], Any] | None = None,
    ) -> dict:
        """调用服务器工具(spec §7.4);返回原始 result(结果治理在 tool.py)。"""
        conn = self._connections.get(name)
        if conn is None or conn.state != McpConnectionState.CONNECTED:
            raise ConnectionError(f'MCP server "{name}" is not connected')
        # 会话过期重试一次:连接缓存失效后自动重连
        for attempt in (0, 1):
            try:
                resp = await conn.transport.send(
                    JsonRpcRequest(
                        id=next_id(),
                        method=MCP_METHODS.TOOLS_CALL,
                        params={"name": tool_name, "arguments": arguments},
                    )
                )
                break
            except McpSessionExpiredError:
                if attempt == 1:
                    raise
                self.invalidate(name)  # 清缓存,下次 send 前重连
                await self.connect_server(name, conn.config)
                conn = self._connections[name]
        if resp.error:
            raise ConnectionError(f'MCP tool {tool_name} error: {resp.error.message}')
        return resp.result or {}

    # ---- 通知 ----

    async def _on_notification(self, name: str, msg: JsonRpcNotification) -> None:
        """处理服务器通知(listChanged 失效缓存;spec §3.3/§6.4)。"""
        method = msg.method
        if method == MCP_METHODS.NOTIFICATION_TOOLS_LIST_CHANGED:
            self.invalidate(name)
        elif method == MCP_METHODS.NOTIFICATION_RESOURCES_LIST_CHANGED:
            self._resources_cache.pop(name, None)
        elif method == MCP_METHODS.NOTIFICATION_PROMPTS_LIST_CHANGED:
            self._commands_cache.pop(name, None)
        if self._notification_handler:
            try:
                await self._notification_handler(name, msg)
            except Exception:  # noqa: BLE001  # 通知处理失败不致命
                logger.warning("notification handler failed for %s", name)

    @staticmethod
    async def _close_transport(conn: McpConnection) -> None:
        try:
            if conn.transport is not None:
                await conn.transport.close()
        except Exception:  # noqa: BLE001
            pass