"""MCP 传输层(spec §4):stdio 本地子进程 + http Streamable HTTP + 工厂。

传输 = 通信管道,管线上跑 JSON-RPC 消息。stdio 起子进程,http 走 httpx POST +
SSE 流。请求/响应按 id 配对,通知经 handler 回调(异步任务)。
参考 `docs/claude-mcp实现.md` §5。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Awaitable, Callable, Protocol

import httpx

from .jsonrpc import (
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    decode,
    encode,
    next_id,
)
from .types import MCP_METHODS, ScopedMcpServerConfig

logger = logging.getLogger(__name__)

#: 协议规定的 POST Accept 头(MCP Streamable HTTP 规范强制,缺失时严格服务器回 406)
_MCP_STREAMABLE_HTTP_ACCEPT = "application/json, text/event-stream"

#: 连接/单次请求超时默认值(毫秒),可用环境变量覆盖
DEFAULT_MCP_TIMEOUT_MS = 30_000

#: 会话过期检测:HTTP 404 + JSON-RPC 错误码 -32001(spec §6.4)
MCP_SESSION_EXPIRED_CODE = -32001

#: stdio 子进程 stderr 收集上限(防内存无界增长,连接失败时输出排查)
_STDERR_CAP = 64 * 1024 * 1024


class BaseMcpTransport(Protocol):
    """传输接口(spec §4.1)。send 为请求-响应;通知经 handler 回调。"""

    async def connect(self) -> None: ...

    async def send(self, msg: JsonRpcRequest) -> JsonRpcResponse: ...

    def set_notification_handler(
        self, handler: Callable[[JsonRpcNotification], Awaitable[None]] | None
    ) -> None: ...

    async def close(self) -> None: ...

    @property
    def stderr_lines(self) -> str: ...


class McpSessionExpiredError(Exception):
    """会话过期(404 + -32001):连接缓存已被清除,上层应重建连接重试一次(spec §6.4)。"""

    def __init__(self, server_name: str) -> None:
        super().__init__(f'MCP server "{server_name}" session expired')
        self.name = "McpSessionExpiredError"


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """日志打码:Authorization 等敏感头一律脱敏(spec §11)。"""
    return {k: "[REDACTED]" if k.lower() in ("authorization", "cookie") else v for k, v in headers.items()}


class StdioTransport(BaseMcpTransport):
    """本地子进程传输:stdin/stdout 走行分隔 JSON,stderr 单独收集(spec §4.2)。"""

    def __init__(
        self,
        *,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        shell_prefix: str | None = None,
        timeout_ms: int = DEFAULT_MCP_TIMEOUT_MS,
    ) -> None:
        self._command = command
        self._args = list(args or [])
        self._env = env
        self._shell_prefix = shell_prefix
        self._timeout_ms = timeout_ms
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[JsonRpcResponse]] = {}
        self._notification_handler: Callable[[JsonRpcNotification], Awaitable[None]] | None = None
        self._stderr_lines: list[str] = []
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False

    async def connect(self) -> None:
        """启动子进程并开始读 stdout/stderr。"""
        cmd = self._command if not self._shell_prefix else self._shell_prefix
        args = [cmd, *self._args] if not self._shell_prefix else [[cmd, *self._args].join(" ")]
        env = {**os.environ, **(self._env or {})}
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        if self._proc.stderr:
            asyncio.create_task(self._read_stderr(self._proc.stderr))

    async def send(self, msg: JsonRpcRequest) -> JsonRpcResponse:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise ConnectionError("transport is not connected")
        future: asyncio.Future[JsonRpcResponse] = asyncio.get_running_loop().create_future()
        self._pending[msg.id] = future
        self._proc.stdin.write((encode(msg) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=self._timeout_ms / 1000)
        except TimeoutError:
            self._pending.pop(msg.id, None)
            raise TimeoutError(f"MCP request {msg.method} timed out after {self._timeout_ms}ms")

    def set_notification_handler(
        self, handler: Callable[[JsonRpcNotification], Awaitable[None]] | None
    ) -> None:
        self._notification_handler = handler

    async def close(self) -> None:
        """优雅关闭:先 SIGINT,再 SIGTERM,最后 SIGKILL(spec §4.2,总上限 500ms)。"""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        # 失败处理:stdin 关闭触发子进程自行退出
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        if sys.platform == "win32":
            # Windows 无 SIGINT/SIGTERM 语义,直接 terminate(taskkill 方案留 16)
            proc.terminate()
        else:
            try:
                proc.terminate()  # SIGTERM(先于 SIGINT 尝试;子进程通常自行处理)
                await asyncio.sleep(0.1)
                if proc.returncode is None:
                    proc.kill()  # SIGKILL 兜底
            except ProcessLookupError:
                pass
        if self._reader_task:
            self._reader_task.cancel()
        # 释放 pending futures,避免挂起
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("transport closed"))
        self._pending.clear()

    async def _read_loop(self) -> None:
        """读 stdout 行,按 id 配对响应,通知转发 handler。"""
        if self._proc is None or self._proc.stdout is None:
            return
        while not self._closed:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                msg = decode(line.decode("utf-8", errors="replace"))
            except ValueError:
                logger.warning("ignoring malformed MCP line from server")
                continue
            if isinstance(msg, JsonRpcResponse) and msg.id in self._pending:
                fut = self._pending.pop(msg.id)
                if not fut.done():
                    fut.set_result(msg)
            elif isinstance(msg, JsonRpcNotification) and self._notification_handler:
                try:
                    await self._notification_handler(msg)
                except Exception:
                    logger.exception("notification handler failed")

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        """收集 stderr(上限 _STDERR_CAP,连接失败时输出排查)。"""
        while not self._closed:
            data = await stream.read(4096)
            if not data:
                break
            text = data.decode("utf-8", errors="replace")
            if len(self._stderr_lines) < _STDERR_CAP:
                self._stderr_lines.append(text)

    @property
    def stderr_lines(self) -> str:
        return "".join(self._stderr_lines)


class HttpTransport(BaseMcpTransport):
    """远程 Streamable HTTP 传输(spec §4.3):httpx POST + SSE 流接收通知。"""

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str] | None = None,
        timeout_ms: int = DEFAULT_MCP_TIMEOUT_MS,
        transport: httpx.AsyncBaseTransport | None = None,  # 测试注入 MockTransport
    ) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._timeout_ms = timeout_ms
        self._client = httpx.AsyncClient(timeout=timeout_ms / 1000, transport=transport)
        self._pending: dict[int, asyncio.Future[JsonRpcResponse]] = {}
        self._notification_handler: Callable[[JsonRpcNotification], Awaitable[None]] | None = None
        self._notify_task: asyncio.Task[None] | None = None
        self._closed = False

    async def connect(self) -> None:
        """连接:建立 GET 常驻流收通知(POST 请求见 send)。"""
        # GET 常驻流不收硬超时(长连接等服务器推送);POST 由 send 单独带超时
        self._notify_task = asyncio.create_task(self._stream_loop())

    async def send(self, msg: JsonRpcRequest) -> JsonRpcResponse:
        """POST 请求并等待匹配 id 的响应(JSON 或 SSE 流)。"""
        if self._closed:
            raise ConnectionError("transport is not connected")
        future: asyncio.Future[JsonRpcResponse] = asyncio.get_running_loop().create_future()
        self._pending[msg.id] = future
        headers = {"Accept": _MCP_STREAMABLE_HTTP_ACCEPT, **self._headers}
        try:
            async with self._client.stream(
                "POST", self._url, content=encode(msg), headers=headers
            ) as resp:
                if resp.status_code == 404:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    if '"code":-32001' in body or '"code": -32001' in body:
                        raise McpSessionExpiredError(str(self._url))
                content_type = resp.headers.get("content-type", "")
                if "text/event-stream" in content_type or "application/x-ndjson" in content_type:
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if line.startswith("data:"):
                            await self._dispatch_line(line[5:].strip())
                else:
                    body = await resp.aread()
                    if body:
                        await self._dispatch_line(body.decode("utf-8", errors="replace"))
        except (httpx.HTTPError, McpSessionExpiredError):
            self._pending.pop(msg.id, None)
            raise
        try:
            return await asyncio.wait_for(future, timeout=self._timeout_ms / 1000)
        except TimeoutError:
            self._pending.pop(msg.id, None)
            raise TimeoutError(f"MCP request {msg.method} timed out after {self._timeout_ms}ms")

    async def _dispatch_line(self, line: str) -> None:
        """解析一行数据(JSON 或 SSE data: 行)并路由到响应/通知。"""
        try:
            msg = decode(line)
        except ValueError:
            return
        if isinstance(msg, JsonRpcResponse) and msg.id in self._pending:
            fut = self._pending.pop(msg.id)
            if not fut.done():
                fut.set_result(msg)
        elif isinstance(msg, JsonRpcNotification) and self._notification_handler:
            try:
                await self._notification_handler(msg)
            except Exception:
                logger.exception("notification handler failed")

    async def _stream_loop(self) -> None:
        """GET 常驻流:服务器推送的通知经此到达(规范规定的 SSE 通道)。"""
        if self._closed:
            return
        try:
            async with self._client.stream("GET", self._url, headers=self._headers) as resp:
                async for line in resp.aiter_lines():
                    if self._closed:
                        break
                    line = line.strip()
                    if line.startswith("data:"):
                        await self._dispatch_line(line[5:].strip())
        except (httpx.HTTPError, asyncio.CancelledError):
            pass

    def set_notification_handler(
        self, handler: Callable[[JsonRpcNotification], Awaitable[None]] | None
    ) -> None:
        self._notification_handler = handler

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._notify_task:
            self._notify_task.cancel()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("transport closed"))
        self._pending.clear()
        await self._client.aclose()

    @property
    def stderr_lines(self) -> str:
        return ""


def create_transport(config: ScopedMcpServerConfig) -> BaseMcpTransport:
    """传输工厂(spec §4.4):按配置类型选择实现。sdk 等其余类型不在本阶段范围。"""
    if config.type in (None, "stdio"):
        return StdioTransport(
            command=config.command or "",
            args=config.args,
            env=config.env,
            shell_prefix=os.environ.get("CLAUDE_CODE_SHELL_PREFIX"),
        )
    if config.type == "http":
        return HttpTransport(url=config.url or "", headers=config.headers)
    raise ValueError(f"unsupported transport: {config.type}")