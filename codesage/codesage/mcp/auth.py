"""MCP OAuth PKCE 认证(spec §9):授权码流程 + PKCE,远程 http 服务器专属。

流程(对照 `docs/claude-mcp实现.md` §9.2):发现 auth 元数据 → 生成 code_verifier/challenge
+ state → 构造授权 URL → 用户浏览器授权(或手动粘贴码)→ 本地回调收 code → 换 token →
存 {config_dir}/mcp/oauth.json → 主动刷新(过期前 5 分钟)+ 吊销。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx

from ..config import atomic, paths
from .types import McpConnection, McpOAuthConfig, ScopedMcpServerConfig

#: token 存储文件
OAUTH_FILE = "oauth.json"
#: 主动刷新阈值:过期前 5 分钟(秒)
REFRESH_THRESHOLD_S = 300


def _oauth_path() -> Path:
    return paths.config_dir() / "mcp" / OAUTH_FILE


def _read_oauth() -> dict[str, Any]:
    """读取 token 存储(缺失/损坏返回空)。"""
    return atomic.read_json_lossy(_oauth_path(), {})


def _write_oauth(data: dict[str, Any]) -> None:
    """原子写 token 存储。"""
    atomic.atomic_write(_oauth_path(), json.dumps(data, indent=2))


def _server_key(config: ScopedMcpServerConfig) -> str:
    """token 槽位键 = 名字 + 配置指纹(同名字改配置 = 新槽,不串;spec §9.2)。"""
    return f"{config.name}|{config.signature() or config.model_dump_json(sort_keys=True)}"


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """生成 (code_verifier, code_challenge);challenge 为 S256 哈希(spec §9.2)。"""
    verifier = _base64url(secrets.token_bytes(32))
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


async def discover_auth_metadata(client: httpx.AsyncClient, config: ScopedMcpServerConfig) -> dict[str, Any]:
    """发现授权服务器元数据(spec §9.2):配置直给或 RFC 8414 标准发现。"""
    oauth = config.oauth
    if oauth and oauth.auth_server_metadata_url:
        resp = await client.get(oauth.auth_server_metadata_url)
        resp.raise_for_status()
        return resp.json()
    # RFC 8414:/.well-known/oauth-authorization-server
    url = config.url or ""
    well_known = f"{url.rstrip('/')}/.well-known/oauth-authorization-server"
    resp = await client.get(well_known)
    if resp.status_code == 200:
        return resp.json()
    raise ConnectionError(f"OAuth metadata discovery failed for {config.name}")


def build_authorization_url(
    *,
    metadata: dict[str, Any],
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
) -> str:
    """构造授权 URL(带 PKCE 参数)。"""
    auth_endpoint = metadata.get("authorization_endpoint")
    if not auth_endpoint:
        raise ConnectionError("no authorization_endpoint in OAuth metadata")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{auth_endpoint}?{urllib.parse.urlencode(params)}"


class OAuthCallbackServer:
    """本地临时回调服务器:收 /callback?code&state 校验后返回 code(spec §9.2)。"""

    def __init__(self, expected_state: str) -> None:
        self._expected_state = expected_state
        self._code: str | None = None
        self._server: asyncio.AbstractServer | None = None
        self._port = 0

    async def start(self) -> tuple[int, str]:
        """启动监听,返回 (port, redirect_uri)。"""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        return port, f"http://localhost:{port}/callback"

    async def _handle(self, reader, writer) -> None:
        request_line = (await reader.readline()).decode("utf-8", errors="replace")
        parts = request_line.split(" ")
        path = parts[1] if len(parts) > 1 else "/"
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<html><body>Authentication complete. You can close this window.</body></html>")
        await writer.drain()
        writer.close()
        state = query.get("state", [""])[0]
        if state == self._expected_state and query.get("code"):
            self._code = query["code"][0]

    async def wait_for_code(self, timeout_s: float = 120.0) -> str:
        """等待授权码(轮询),超时抛 TimeoutError。"""
        elapsed = 0.0
        while self._code is None and elapsed < timeout_s:
            await asyncio.sleep(0.2)
            elapsed += 0.2
        if self._code is None:
            raise TimeoutError("OAuth authorization timed out")
        return self._code

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


async def exchange_code_for_tokens(
    client: httpx.AsyncClient,
    metadata: dict[str, Any],
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """用授权码换 token(带 code_verifier)。"""
    token_endpoint = metadata.get("token_endpoint")
    if not token_endpoint:
        raise ConnectionError("no token_endpoint in OAuth metadata")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    resp = await client.post(token_endpoint, data=data)
    if resp.status_code >= 400:
        raise ConnectionError(f"token exchange failed: {resp.status_code} {resp.text}")
    return resp.json()


def save_tokens(config: ScopedMcpServerConfig, tokens: dict[str, Any]) -> None:
    """持久化 token(spec §9.2)。"""
    data = _read_oauth()
    key = _server_key(config)
    data[key] = {
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": __import__("time").time() + int(tokens.get("expires_in", 3600)),
        "scope": tokens.get("scope"),
        "client_id": tokens.get("client_id") or (config.oauth.client_id if config.oauth else None),
    }
    _write_oauth(data)


def get_tokens(config: ScopedMcpServerConfig) -> dict[str, Any] | None:
    """读 token(供 http 传输带 Bearer)。"""
    return _read_oauth().get(_server_key(config))


async def refresh_tokens(client: httpx.AsyncClient, config: ScopedMcpServerConfig, metadata: dict[str, Any]) -> dict[str, Any] | None:
    """刷新 token(spec §9.3):用 refresh_token 换新 token 并回写;invalid_grant 清 token。"""
    key = _server_key(config)
    tokens = get_tokens(config)
    refresh_token = tokens and tokens.get("refresh_token")
    if not refresh_token:
        return None
    token_endpoint = metadata.get("token_endpoint")
    if not token_endpoint:
        return None
    client_id = (config.oauth and config.oauth.client_id) or (tokens and tokens.get("client_id"))
    if not client_id:
        return None
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    resp = await client.post(token_endpoint, data=data)
    if resp.status_code >= 400:
        _clear_tokens(config)
        return None
    new = resp.json()
    save_tokens(config, new)
    return new


def _clear_tokens(config: ScopedMcpServerConfig) -> None:
    data = _read_oauth()
    data.pop(_server_key(config), None)
    _write_oauth(data)


def ensure_valid_token(config: ScopedMcpServerConfig) -> str | None:
    """取有效 access_token;过期前 5 分钟标记需刷新(调用方刷新;spec §9.3)。"""
    tokens = get_tokens(config)
    if not tokens or not tokens.get("access_token"):
        return None
    expires_at = tokens.get("expires_at", 0)
    if expires_at and (expires_at - __import__("time").time()) <= REFRESH_THRESHOLD_S:
        return None  # 即将过期,调用方触发刷新
    return tokens.get("access_token")


async def authenticate(
    config: ScopedMcpServerConfig,
    *,
    client: httpx.AsyncClient | None = None,
    open_browser: bool = True,
) -> str:
    """执行完整 OAuth 授权码 + PKCE 流程(spec §9.2),返回访问成功提示。

    open_browser=False(如 --print 模式):打印 URL 让用户手动打开并粘贴授权码。
    """
    client = client or httpx.AsyncClient()
    metadata = await discover_auth_metadata(client, config)
    oauth = config.oauth or McpOAuthConfig()
    client_id = oauth.client_id or "codesage"
    verifier, challenge = generate_pkce()
    state = _base64url(secrets.token_bytes(16))

    cb = OAuthCallbackServer(state)
    port, redirect_uri = await cb.start()
    url = build_authorization_url(
        metadata=metadata, client_id=client_id, redirect_uri=redirect_uri,
        code_challenge=challenge, state=state,
    )
    try:
        if open_browser:
            webbrowser.open(url)
        else:
            print(f"请打开以下 URL 授权 {config.name} MCP 服务器,然后粘贴重定向到的地址:\n{url}\n")
            import sys

            print("授权完成后,请在此粘贴浏览器地址栏中的完整 URL:")
            pasted = (await asyncio.to_thread(sys.stdin.readline)).strip()
            code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [""])[0]
            if not code:
                raise ConnectionError("no code in pasted URL")
        code = await cb.wait_for_code()
        tokens = await exchange_code_for_tokens(client, metadata, client_id, code, verifier, redirect_uri)
        save_tokens(config, tokens)
        return f"{config.name} MCP 服务器已认证"
    finally:
        await cb.close()


def needs_auth(config: ScopedMcpServerConfig) -> bool:
    """判断是否需认证(远程 http 且无有效 token;spec §9.5)。"""
    if config.type != "http":
        return False
    return ensure_valid_token(config) is None