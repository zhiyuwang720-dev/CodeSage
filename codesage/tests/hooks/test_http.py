"""HTTP 执行体测试(§9.1 test_http.py):URL 白名单(空 [] 全禁 / * 通配 / 未命中)、
header 插值白名单($HOME 不可替换)、CRLF 消毒、SSRF 矩阵(内网/云元数据/链路本地
拒绝、127.0.0.1 放行)、非 2xx → fail-closed、非 JSON body → fail-closed、
空 body → {} 成功、超时 → fail-closed、max_redirects=0。

httpx.MockTransport 注入,不真发请求;SSRF 的域名解析路径用 monkeypatch 注入
(不依赖真实 DNS)。"""

import asyncio
import json
import os

import httpx
import pytest

from codesage.hooks import HookValidationError
from codesage.hooks import http as http_mod
from codesage.hooks.command import parse_hook_stdout
from codesage.hooks.http import (
    HttpHookExecutor,
    HookExecutionError,
    interpolate_header_value,
)

# HookInput 序列化(§2.1 基础三字段)
INPUT = json.dumps({"session_id": "s1", "cwd": "C:/proj", "session_path": "C:/proj/s.jsonl"})
URL = "http://127.0.0.1:8000/hook"


def _executor(url: str = URL, *, handler=None, urls_whitelist=None, **kwargs) -> HttpHookExecutor:
    """构造注入 MockTransport 的 executor(handler 返回 httpx.Response)。"""
    if handler is None:
        handler = lambda request: httpx.Response(200, json={})  # noqa: E731
    return HttpHookExecutor(
        url,
        urls_whitelist=urls_whitelist if urls_whitelist is not None else ["*"],
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# URL 白名单(§4.9):默认 [] 全禁 / * 通配 / 未命中 → 不执行


async def test_whitelist_empty_all_blocked():
    """§4.9:白名单为空(= 默认)→ 全部拒绝,不发出请求。"""
    requests: list = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    ex = _executor(urls_whitelist=[], handler=handler)
    with pytest.raises(HookExecutionError, match="whitelist"):
        await ex.run(INPUT, timeout=5)
    assert requests == []  # 未执行


async def test_whitelist_wildcard():
    """§4.9:`*` 通配匹配 → 放行。"""
    r = await _executor().run(INPUT, timeout=5)
    assert r.exit_code == 0


async def test_whitelist_pattern_hit():
    """§4.9:fnmatch 模式命中(路径前缀通配)。"""
    ex = _executor(
        URL, urls_whitelist=["http://127.0.0.1:8000/*"], handler=lambda req: httpx.Response(200)
    )
    r = await ex.run(INPUT, timeout=5)
    assert r.exit_code == 0


async def test_whitelist_miss_not_executed():
    """§4.9:URL 未命中白名单 → 不执行 + HookExecutionError(解析期 warning 在 S2)。"""
    ex = _executor(
        "http://127.0.0.1:9999/other",
        urls_whitelist=["http://127.0.0.1:8000/*"],
        handler=lambda req: httpx.Response(200),
    )
    with pytest.raises(HookExecutionError, match="whitelist"):
        await ex.run(INPUT, timeout=5)


# ---------------------------------------------------------------------------
# header 插值白名单(§4.9):未列入 → 空字符串;CRLF 消毒


def test_header_interpolation_whitelist(monkeypatch):
    """§4.9:$VAR/${VAR} 仅替换 allowedEnvVars 白名单内变量;未列入 → 空字符串。"""
    monkeypatch.setenv("TOKEN", "abc123")
    monkeypatch.setenv("HOME", "/home/evil")
    assert interpolate_header_value("Bearer $TOKEN", ["TOKEN"]) == "Bearer abc123"
    assert interpolate_header_value("Bearer ${TOKEN}", ["TOKEN"]) == "Bearer abc123"
    assert interpolate_header_value("$HOME", ["TOKEN"]) == ""  # 未列入 → 空字符串
    assert interpolate_header_value("$TOKEN $HOME", ["TOKEN"]) == "abc123 "
    assert interpolate_header_value("no vars", ["TOKEN"]) == "no vars"
    assert interpolate_header_value("$MISSING", ["MISSING"]) == ""  # 白名单内但未设置


def test_header_interpolation_crlf_sanitized(monkeypatch):
    """§4.9:插值后剥离 \r\n\x00(含静态部分,对最终值整体消毒)。"""
    # Windows os.environ 拒绝 \x00 值,换成普通 dict 注入(monkeypatch 自动还原)
    monkeypatch.setattr(os, "environ", {**os.environ, "EVIL": "x\r\nInjected: yes\x00"})
    assert interpolate_header_value("$EVIL", ["EVIL"]) == "xInjected: yes"
    assert interpolate_header_value("a\rb\nc\x00d", ["EVIL"]) == "abcd"


async def test_header_interpolation_sent_on_wire(monkeypatch):
    """§4.9:插值结果实际随请求发出;未列入变量 → 空字符串上 wire。"""
    monkeypatch.setenv("TOKEN", "t-42")
    monkeypatch.setenv("HOME", "H")
    seen: dict = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["home"] = request.headers.get("X-Home")
        seen["braced"] = request.headers.get("X-Braced")
        return httpx.Response(200)

    ex = _executor(
        headers={
            "Authorization": "Bearer $TOKEN",
            "X-Home": "$HOME",  # 未列入 allowedEnvVars → 空字符串
            "X-Braced": "${TOKEN}",
        },
        allowed_env_vars=["TOKEN"],
        handler=handler,
    )
    await ex.run(INPUT, timeout=5)
    assert seen["auth"] == "Bearer t-42"
    assert seen["home"] == ""
    assert seen["braced"] == "t-42"


# ---------------------------------------------------------------------------
# SSRF 矩阵(§4.9):内网 / 云元数据 / 链路本地 / 0.0.0.0 / :: 拒绝;loopback 放行


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/hook",  # 10/8
        "http://172.16.0.1/hook",  # 172.16/12
        "http://192.168.1.1/hook",  # 192.168/16
        "http://169.254.169.254/latest/meta-data/",  # 169.254/16 云元数据
        "http://100.64.0.1/hook",  # 100.64/10 CGNAT/云元数据
        "http://0.0.0.0/hook",  # 0/8
        "http://[::]/hook",  # IPv6 未指定
        "http://[fc00::1]/hook",  # fc00::/7 ULA
        "http://[fe80::1]/hook",  # fe80::/10 链路本地
    ],
)
async def test_ssrf_blocked_matrix(url):
    """§4.9:内网/云元数据/链路本地/未指定地址全拒(白名单 `*` 也拦不住 SSRF)。"""
    ex = _executor(url, handler=lambda req: httpx.Response(200))
    with pytest.raises(HookExecutionError, match="SSRF"):
        await ex.run(INPUT, timeout=5)


async def test_ssrf_loopback_allowed():
    """§4.9:放行 loopback 127.0.0.1(本地策略服务器是真实场景)。"""
    r = await _executor().run(INPUT, timeout=5)
    assert r.exit_code == 0


async def test_ssrf_ipv4_mapped_ipv6_blocked():
    """§4.9(实现补强):::ffff:10.0.0.1 降级为 IPv4 判定,仍拦内网。"""
    ex = _executor("http://[::ffff:10.0.0.1]/hook", handler=lambda req: httpx.Response(200))
    with pytest.raises(HookExecutionError, match="SSRF"):
        await ex.run(INPUT, timeout=5)


async def test_ssrf_hostname_resolves_to_loopback_allowed():
    """§4.9:域名解析路径 —— localhost → loopback → 放行(真实 getaddrinfo)。"""
    r = await _executor("http://localhost:8000/hook").run(INPUT, timeout=5)
    assert r.exit_code == 0


async def test_ssrf_hostname_resolves_to_private_blocked(monkeypatch):
    """§4.9:域名解析到内网 → 拒绝(monkeypatch 注入解析结果,不依赖真实 DNS)。"""
    async def fake_resolve(host):
        return ["10.0.0.1"]

    monkeypatch.setattr(http_mod, "_resolve", fake_resolve)
    ex = _executor("http://evil.example/hook", handler=lambda req: httpx.Response(200))
    with pytest.raises(HookExecutionError, match="SSRF"):
        await ex.run(INPUT, timeout=5)


async def test_ssrf_hostname_resolves_to_loopback_deterministic(monkeypatch):
    """§4.9:域名解析到 loopback → 放行(fake _resolve,确定性,不依赖真实 DNS)。"""
    async def fake_resolve(host):
        return ["127.0.0.1"]

    monkeypatch.setattr(http_mod, "_resolve", fake_resolve)
    r = await _executor("http://fakehost.example/hook").run(INPUT, timeout=5)
    assert r.exit_code == 0


async def test_ssrf_resolution_fail_closed_branches(monkeypatch):
    """§4.9:域名解析的三条 fail-closed 分支 —— 解析失败 / 空结果 / 不可分类地址,全拒。"""
    async def fake_resolve(host):
        raise OSError("DNS failure")

    async def fake_resolve_empty(host):
        return []

    async def fake_resolve_garbage(host):
        return ["not-an-address"]

    ex = _executor("http://evil.example/hook", handler=lambda req: httpx.Response(200))
    monkeypatch.setattr(http_mod, "_resolve", fake_resolve)
    with pytest.raises(HookExecutionError, match="resolution"):
        await ex.run(INPUT, timeout=5)
    monkeypatch.setattr(http_mod, "_resolve", fake_resolve_empty)
    with pytest.raises(HookExecutionError, match="resolution"):
        await ex.run(INPUT, timeout=5)
    monkeypatch.setattr(http_mod, "_resolve", fake_resolve_garbage)
    with pytest.raises(HookExecutionError, match="classify"):
        await ex.run(INPUT, timeout=5)


async def test_ssrf_bad_scheme_and_host(monkeypatch):
    """§4.9 前置:非 http(s) scheme / 无 hostname → 拒绝执行。"""
    async def fake_resolve(host):
        return ["1.2.3.4"]

    monkeypatch.setattr(http_mod, "_resolve", fake_resolve)
    with pytest.raises(HookExecutionError, match="scheme"):
        await _executor("ftp://127.0.0.1/hook").run(INPUT, timeout=5)
    with pytest.raises(HookExecutionError, match="hostname"):
        await _executor("http:///nohost").run(INPUT, timeout=5)


# ---------------------------------------------------------------------------
# 请求形状(§4.9):POST + HookInput JSON body + Content-Type + 响应解析


async def test_post_body_content_type_and_json_parse():
    """§4.9:POST,body = HookInput JSON,Content-Type: application/json;
    2xx + JSON body → stdout 原样返回,S5 侧 parse_hook_stdout 可解析。"""
    seen: dict = {}

    def handler(request):
        seen["method"] = request.method
        seen["content"] = request.content
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"permissionDecision": "deny", "permissionDecisionReason": "r"})

    r = await _executor(handler=handler).run(INPUT, timeout=5)
    assert seen["method"] == "POST"
    assert seen["content"] == INPUT.encode("utf-8")
    assert seen["content_type"] == "application/json"
    assert r.exit_code == 0
    out, warnings = parse_hook_stdout(r.stdout, "PreToolUse")
    assert warnings == []
    assert out.permissionDecision == "deny"


async def test_empty_body_means_success():
    """§4.9:空 body → {} 成功(无决策空对象)。"""
    r = await _executor(handler=lambda req: httpx.Response(200)).run(INPUT, timeout=5)
    assert r.exit_code == 0
    assert r.stdout == "{}"
    out, _ = parse_hook_stdout(r.stdout, "PreToolUse")
    assert out.permissionDecision is None


async def test_non_json_body_fail_closed():
    """§4.9:非空且不以 { 开头 → fail-closed(HookValidationError)。"""
    ex = _executor(handler=lambda req: httpx.Response(200, text="not json at all"))
    with pytest.raises(HookValidationError, match="JSON"):
        await ex.run(INPUT, timeout=5)


async def test_invalid_json_body_fail_closed():
    """§4.9:以 { 开头但 JSON 非法 → fail-closed(HookValidationError)。"""
    ex = _executor(handler=lambda req: httpx.Response(200, text="{broken json"))
    with pytest.raises(HookValidationError, match="JSON"):
        await ex.run(INPUT, timeout=5)


async def test_leading_whitespace_json_body_accepted():
    """§4.9:前导空白宽容(同 parse_hook_stdout),不当 plainText 放行。"""
    r = await _executor(handler=lambda req: httpx.Response(200, text='\n{"permissionDecision": "allow"}')).run(
        INPUT, timeout=5
    )
    out, _ = parse_hook_stdout(r.stdout, "PreToolUse")
    assert out.permissionDecision == "allow"


async def test_body_truncated_over_256kb_fail_closed():
    """§4.10.5:响应体超 256KB 截断;截断的 JSON 自然解析失败 → fail-closed。"""
    ex = _executor(handler=lambda req: httpx.Response(200, content=b"{" + b"x" * 300_000))
    with pytest.raises(HookValidationError):
        await ex.run(INPUT, timeout=5)


# ---------------------------------------------------------------------------
# 非 2xx / 重定向 / 网络错误 / 超时(§4.6 fail-closed 依据)


async def test_non_2xx_fail_closed():
    """§4.9:500 → HookExecutionError(§4.6 表,PreToolUse → deny 由 S5 消费)。"""
    ex = _executor(handler=lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(HookExecutionError, match="500"):
        await ex.run(INPUT, timeout=5)


async def test_max_redirects_zero_not_followed():
    """§4.9:max_redirects=0 —— 302 不跟随,只见一次请求 + 非 2xx 失败。"""
    requests: list = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/other"})

    ex = _executor(handler=handler)
    with pytest.raises(HookExecutionError, match="302"):
        await ex.run(INPUT, timeout=5)
    assert len(requests) == 1  # 重定向未被跟随


async def test_network_error_fail_closed():
    """§4.9:传输层错误 → HookExecutionError(§4.6 表同 command spawn 失败档)。"""
    def handler(request):
        raise httpx.ConnectError("connection refused")

    ex = _executor(handler=handler)
    with pytest.raises(HookExecutionError, match="request failed"):
        await ex.run(INPUT, timeout=5)


async def test_timeout_fail_closed():
    """§4.2/§4.6:响应挂起超时 → TimeoutError(fail-closed 依据,不拖到超时值)。"""
    async def handler(request):
        await asyncio.sleep(10)
        return httpx.Response(200)

    ex = _executor(handler=handler)
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError, match="timed out"):
        await ex.run(INPUT, timeout=0.5)
    assert asyncio.get_running_loop().time() - started < 5  # 不等 10s 挂起
