"""MCP OAuth 测试(spec 12.1:test_auth.py)。

覆盖:PKCE 生成/授权码换 token/元数据发现/主动刷新/清 token 失效。
用 httpx.MockTransport 模拟授权服务器。
"""

import asyncio
import base64
import hashlib

import httpx
import pytest

from codesage.mcp import auth
from codesage.mcp.auth import (
    build_authorization_url,
    discover_auth_metadata,
    exchange_code_for_tokens,
    generate_pkce,
    get_tokens,
    refresh_tokens,
    save_tokens,
)
from codesage.mcp.types import ConfigScope, ScopedMcpServerConfig


def make_http_cfg(name="remote", **kw) -> ScopedMcpServerConfig:
    return ScopedMcpServerConfig(
        name=name, scope=ConfigScope.LOCAL, type="http",
        url=kw.pop("url", "https://api.example.com/mcp"), **kw,
    )


def test_generate_pkce():
    """spec §9.2:verifier/challenge 正确(S256)。"""
    verifier, challenge = generate_pkce()
    assert len(verifier) >= 32
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_build_authorization_url():
    """spec §9.2:授权 URL 带 PKCE/state/redirect 参数。"""
    url = build_authorization_url(
        metadata={"authorization_endpoint": "https://auth.example.com/authorize"},
        client_id="cid", redirect_uri="http://localhost:8080/callback",
        code_challenge="challenge", state="state123",
    )
    assert "authorization_endpoint" not in url
    assert "https://auth.example.com/authorize" in url
    assert "client_id=cid" in url
    assert "code_challenge=challenge" in url
    assert "code_challenge_method=S256" in url
    assert "state=state123" in url


@pytest.mark.asyncio
async def test_discover_metadata_via_well_known():
    """spec §9.2:标准发现 /.well-known。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/.well-known/oauth-authorization-server")
        return httpx.Response(200, json={"authorization_endpoint": "https://auth.example.com/authorize", "token_endpoint": "https://auth.example.com/token"})

    cfg = make_http_cfg()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        meta = await discover_auth_metadata(client, cfg)
    assert meta["authorization_endpoint"].endswith("/authorize")


@pytest.mark.asyncio
async def test_exchange_code_for_tokens():
    """spec §9.2:授权码换 token 带 code_verifier。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})

    cfg = make_http_cfg()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        meta = {"token_endpoint": "https://auth.example.com/token"}
        tokens = await exchange_code_for_tokens(client, meta, "cid", "code123", "verifier123", "http://localhost/cb")
    assert tokens["access_token"] == "at"
    assert "code_verifier=verifier123" in seen["body"]


def test_save_and_get_tokens_roundtrip():
    """spec §9.2:token 持久化与读取。"""
    cfg = make_http_cfg()
    save_tokens(cfg, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "read"})
    tokens = get_tokens(cfg)
    assert tokens["access_token"] == "at"
    assert tokens["refresh_token"] == "rt"


def test_get_tokens_expired_returns_none():
    """spec §9.3:即将过期(5 分钟内)视为无效,触发刷新。"""
    cfg = make_http_cfg()
    save_tokens(cfg, {"access_token": "at", "expires_in": 1})  # 1 秒即过期
    assert auth.ensure_valid_token(cfg) is None


def test_needs_auth_only_http():
    """spec §9.5:仅远程 http 需认证。"""
    stdio_cfg = ScopedMcpServerConfig(name="s", scope=ConfigScope.LOCAL, command="echo")
    assert auth.needs_auth(stdio_cfg) is False
    http_cfg = make_http_cfg()  # 无 token → 需认证
    assert auth.needs_auth(http_cfg) is True


@pytest.mark.asyncio
async def test_refresh_tokens_success():
    """spec §9.3:刷新成功并回写。"""
    cfg = make_http_cfg()
    save_tokens(cfg, {"access_token": "old", "refresh_token": "rt", "expires_in": 3600, "client_id": "cid"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "rt2", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        meta = {"token_endpoint": "https://auth.example.com/token"}
        new = await refresh_tokens(client, cfg, meta)
    assert new["access_token"] == "new"
    assert get_tokens(cfg)["access_token"] == "new"  # 已回写


@pytest.mark.asyncio
async def test_refresh_tokens_invalid_grant_clears():
    """spec §9.3:invalid_grant 清 token。"""
    cfg = make_http_cfg()
    save_tokens(cfg, {"access_token": "old", "refresh_token": "bad", "expires_in": 3600, "client_id": "cid"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        meta = {"token_endpoint": "https://auth.example.com/token"}
        new = await refresh_tokens(client, cfg, meta)
    assert new is None
    assert get_tokens(cfg) is None  # token 已清