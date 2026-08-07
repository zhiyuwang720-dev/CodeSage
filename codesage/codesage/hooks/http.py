"""HTTP 执行体(阶段 09,S4):POST + 白名单/SSRF/插值消毒 + 必须 JSON 契约,§4.9。

复用既有 httpx(零新依赖);不共享 LLMClient 的 client 实例(带 VCR transport,
语义不符,§4.9 执行体隔离)。四层安全防护在此落地:URL 白名单(默认 [] 全禁,
配置解析期 S2 已拦一次,执行期再拒双保险)、SSRF 矩阵(ipaddress 标准库)、
header 白名单插值、CRLF 消毒。§4.6 fail-closed 语义(PreToolUse → deny)由
S5 HookManager 消费:本模块对非 2xx/网络错误抛 HookExecutionError、超时抛
TimeoutError、非 JSON body 抛 HookValidationError(HTTP 无 command 的
plainText 分支,必须返回 JSON)。

仅 SessionStart 禁用(§4.9 事件适配)由 S5 分发层执行,本类不感知事件名。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

import httpx

from ._common import url_allowed
from .base import HookResult
from .command import MAX_STDOUT_BYTES, HookExecutionError
from .types import HookValidationError

logger = logging.getLogger("codesage.hooks")

#: SSRF 阻断网段(§4.9,照抄 CC ssrfGuard):0/8、10/8、100.64/10(CGNAT/云元数据)、
#: 169.254/16、172.16/12、192.168/16;loopback 127.0.0.1 放行(本地策略服务器是真实场景)。
_BLOCKED_IPV4 = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]
#: IPv6:fc00::/7(ULA)、fe80::/10(链路本地)、::(未指定)。
_BLOCKED_IPV6 = [
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::/128"),
]

#: header 值插值模式(§4.9):$VAR 与 ${VAR} 两种形式
_ENV_VAR_RE = re.compile(r"\$(\w+)|\$\{(\w+)\}")


def _is_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """SSRF 判定(§4.9):loopback 放行;阻断网段命中即拒。

    IPv4-mapped IPv6(如 ::ffff:10.0.0.1)先降级为 IPv4 再判——否则可绕过 IPv4
    阻断网段(spec 未显式列此项,实现补强,见 S4 报告)。
    """
    if addr.is_loopback:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return _is_blocked(addr.ipv4_mapped)
    if isinstance(addr, ipaddress.IPv4Address):
        return any(addr in net for net in _BLOCKED_IPV4)
    return any(addr in net for net in _BLOCKED_IPV6)


async def _resolve(host: str) -> list[str]:
    """解析 host 的全部地址(§4.9:域名先解析再校验;测试可替换此函数注入解析结果)。"""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    return [info[4][0] for info in infos]


async def _check_ssrf(url: str) -> None:
    """SSRF 校验(§4.9):URL host 为 IP → 直接判定;为域名 → 解析后逐地址判定
    (任一命中阻断即拒,防多地址解析绕过)。DNS 重绑定(校验与连接间地址变化)的残余
    TOCTOU 窗口由 URL 白名单 + 配置快照语义兜底,v1 不做连接钉扎
    (ponytail: 升级路径 = 自定义 transport 钉扎已校验地址)。
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise HookExecutionError(f"http hook url parse failed: {exc}") from exc
    if parts.scheme not in ("http", "https"):
        raise HookExecutionError(
            f"http hook url must be http/https, got scheme {parts.scheme!r}"
        )
    host = parts.hostname
    if not host:
        raise HookExecutionError(f"http hook url {url!r} has no hostname")
    try:
        addr = ipaddress.ip_address(host.strip("[]"))  # IPv6 字面量 [::1] 去括号
    except ValueError:
        pass  # 域名 → 解析后逐地址校验
    else:
        if _is_blocked(addr):
            raise HookExecutionError(f"SSRF guard blocked (§4.9): {host}")
        return
    try:
        addrs = await _resolve(host)
    except OSError as exc:
        raise HookExecutionError(f"http hook hostname resolution failed: {host}: {exc}") from exc
    if not addrs:
        raise HookExecutionError(f"http hook hostname resolution failed: {host}")
    for addr in addrs:
        try:
            checked = ipaddress.ip_address(addr.split("%", 1)[0])  # 去链路本地 scope
        except ValueError:
            # 解析器返回的不可分类地址:fail-closed,不当白放
            raise HookExecutionError(
                f"SSRF guard could not classify resolved address {addr!r} for {host}"
            )
        if _is_blocked(checked):
            raise HookExecutionError(f"SSRF guard blocked (§4.9): {host} → {addr}")


def interpolate_header_value(value: str, allowed_env_vars: list[str] | None) -> str:
    """header 值插值 + CRLF 消毒(§4.9):`$VAR`/`${VAR}` 仅替换 allowedEnvVars
    白名单内变量,未列入 → 空字符串(防 $HOME/$AWS_SECRET_ACCESS_KEY 泄漏);
    插值后剥离 `\\r\\n\\x00`(对最终值整体消毒——静态部分同样剥离,防配置注入)。"""
    allowed = set(allowed_env_vars or [])

    def _repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return os.environ.get(name, "") if name in allowed else ""

    value = _ENV_VAR_RE.sub(_repl, value)
    return value.replace("\r", "").replace("\n", "").replace("\x00", "")


class HttpHookExecutor:
    """HTTP 执行体(§4.9):POST + max_redirects=0 + 必须 JSON 契约 + 60s 超时。

    安全序(§4.9):URL 白名单(默认 [] 全禁)→ SSRF 校验 → header 插值 → CRLF 消毒,
    全部在请求发出前。响应:空 body → `{}` 成功;非空必须以 `{` 开头且 JSON 合法,
    否则 HookValidationError(HTTP 无 plainText 分支,§4.9 与 command 的分歧)。
    非 2xx / 网络错误 → HookExecutionError、超时 → TimeoutError,由 S5 按 §4.6
    表 fail-closed(PreToolUse → deny)。结构校验在此、事件级 schema 校验在 S5
    (parse_hook_stdout,事件名不落本类)。

    transport 参数仅测试注入(httpx.MockTransport,不真发请求);生产路径为 None。
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        allowed_env_vars: list[str] | None = None,
        urls_whitelist: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.allowed_env_vars = allowed_env_vars
        # None 视同空白名单 = 全禁(§4.9 默认值,fail-closed 取向)
        self.urls_whitelist = urls_whitelist or []
        self._transport = transport

    async def run(self, input_json: str, *, timeout: float) -> HookResult:
        if not url_allowed(self.url, self.urls_whitelist):
            # §4.9:未命中白名单 → 不执行(S2 解析期已拦一次,此处双保险)
            raise HookExecutionError(
                f"http hook url {self.url!r} not in http_hook_urls whitelist: not executed (§4.9)"
            )
        await _check_ssrf(self.url)

        headers: dict[str, str] = {"Content-Type": "application/json"}  # body = HookInput JSON
        for name, value in self.headers.items():
            headers[name] = interpolate_header_value(value, self.allowed_env_vars)

        started = time.monotonic()
        try:
            # 每请求新建 client:不共享 LLMClient 的带 VCR transport 实例(§4.9 执行体隔离)
            async with httpx.AsyncClient(
                timeout=timeout,
                transport=self._transport,
                trust_env=True,  # 代理走 httpx 默认(HTTP_PROXY/HTTPS_PROXY/NO_PROXY),无 CC 的 sandbox 代理层(差异文档化,B3)
            ) as client:
                # follow_redirects=False = max_redirects=0(§4.9:重定向不跟随);
                # wait_for 兜底超时(§4.2),httpx 自身超时同一时刻失效,两者都映射为 TimeoutError
                resp = await asyncio.wait_for(
                    client.post(
                        self.url,
                        content=input_json.encode("utf-8"),
                        headers=headers,
                        follow_redirects=False,
                    ),
                    timeout,
                )
        except (TimeoutError, httpx.TimeoutException):
            raise TimeoutError(
                f"http hook timed out after {timeout:.1f}s: {self.url!r}"
            ) from None
        except httpx.HTTPError as exc:
            # 网络错误(DNS/连接/TLS/协议)→ §4.6 表 fail-closed
            raise HookExecutionError(f"http hook request failed: {exc}") from exc

        if not 200 <= resp.status_code < 300:
            # 非 2xx(含 3xx:max_redirects=0 不跟随)→ 同 §4.6 表
            raise HookExecutionError(
                f"http hook returned {resp.status_code}: {self.url!r}"
            )

        body = resp.content
        if len(body) > MAX_STDOUT_BYTES:
            # 捕获限额(§4.10.5):截断的 JSON 自然解析失败 → fail-closed
            logger.warning("http hook body exceeded %d bytes: truncated (§4.10.5)", MAX_STDOUT_BYTES)
            body = body[:MAX_STDOUT_BYTES]
        text = body.decode("utf-8", errors="replace")
        if not text.strip():
            text = "{}"  # §4.9:空 body → {} 成功
        if not text.lstrip().startswith("{"):
            # HTTP 必须返回 JSON:非空不以 { 开头 → fail-closed(§4.9,与 command 的
            # plainText 分支刻意分歧;前导空白宽容,同 parse_hook_stdout)
            raise HookValidationError(
                f"http hook response must be a JSON object, got: {text[:200]!r}"
            )
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise HookValidationError(f"http hook returned invalid JSON: {exc}") from exc

        return HookResult(
            exit_code=0,  # HTTP 无退出码:2xx + JSON 合法 = 成功(§4.3 0 同义)
            stdout=text,
            stderr="",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
