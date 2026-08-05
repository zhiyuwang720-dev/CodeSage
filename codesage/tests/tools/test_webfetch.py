"""WebFetch tool tests: SSRF gates + fetch semantics via injected MockTransport
client and a stubbed DNS resolver (no real network, no real DNS)."""

import socket

import httpx
import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.network.webfetch import WebFetchTool, check_url

PUBLIC_IP = "93.184.216.34"  # example.com

TEST_DNS = {
    "example.com": [PUBLIC_IP],
    "localhost": ["127.0.0.1"],
    "127.0.0.1": ["127.0.0.1"],
    "10.0.0.5": ["10.0.0.5"],
    "192.168.1.10": ["192.168.1.10"],
    "intra.corp": ["10.0.0.5"],  # dotted name resolving to a private IP
}


def _fake_getaddrinfo(host, *args, **kwargs):
    ips = TEST_DNS.get(host)
    if ips is None:
        raise socket.gaierror(-2, "Name or service not known")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80)) for ip in ips]


@pytest.fixture
def no_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


def _tool(handler) -> WebFetchTool:
    return WebFetchTool(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def _run(tool, url, tmp_path):
    return await tool.call({"url": url}, _ctx(tmp_path)).__anext__()


# --- SSRF gates (checked before any request is made) ------------------------


@pytest.mark.asyncio
async def test_private_ips_rejected(no_dns, tmp_path):
    for url in (
        "http://127.0.0.1:8000/",
        "http://10.0.0.5/",
        "http://192.168.1.10/",
        "http://localhost/",  # bare name allowed by dot check, refused by IP check
    ):
        result = await _run(WebFetchTool(), url, tmp_path)
        assert result.is_error, url
        assert "private" in result.content, url


@pytest.mark.asyncio
async def test_host_resolving_to_private_ip_rejected(no_dns, tmp_path):
    result = await _run(WebFetchTool(), "http://intra.corp/", tmp_path)
    assert result.is_error
    assert "private" in result.content


@pytest.mark.asyncio
async def test_credentials_rejected(no_dns, tmp_path):
    result = await _run(WebFetchTool(), "http://user:pass@example.com/", tmp_path)
    assert result.is_error
    assert "credentials" in result.content


@pytest.mark.asyncio
async def test_bad_scheme_and_unresolvable_rejected(no_dns, tmp_path):
    assert "only http/https" in (await _run(WebFetchTool(), "ftp://example.com/", tmp_path)).content
    result = await _run(WebFetchTool(), "http://nonexistent-host.example/", tmp_path)
    assert result.is_error
    assert "resolve" in result.content


def test_is_private_ip_ranges(no_dns):
    from codesage.tools.builtin.network.webfetch import _is_private_ip

    for ip in ("10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.0.1", "127.0.0.1",
               "169.254.1.1", "0.0.0.0", "::1", "fc00::1", "fe80::1", "::ffff:10.0.0.1",
               "240.0.0.1"):
        assert _is_private_ip(ip), ip
    for ip in ("8.8.8.8", "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"):
        assert not _is_private_ip(ip), ip


# --- fetch semantics (MockTransport, DNS stubbed to a public IP) -------------


@pytest.mark.asyncio
async def test_fetch_html_converts_to_markdown(no_dns, tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"""\
<html><head><title>Test Page</title></head><body>
<h1>Hello</h1>
<p>First para with <a href="/x">a link</a>.</p>
<ul><li>item one</li><li>item two</li></ul>
</body></html>""")

    result = await _run(_tool(handler), "http://example.com/", tmp_path)
    assert not result.is_error
    assert "# Test Page" in result.content
    assert "# Hello" in result.content
    assert "First para with" in result.content
    assert "[a link](/x)" in result.content
    assert "- item one" in result.content
    assert calls == ["http://example.com/"]


@pytest.mark.asyncio
async def test_fetch_plain_text_passthrough(no_dns, tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"just text")

    result = await _run(_tool(handler), "http://example.com/raw", tmp_path)
    assert not result.is_error
    assert result.content == "just text"


@pytest.mark.asyncio
async def test_redirect_is_error_not_followed(no_dns, tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(302, headers={"location": "https://other.example/page"})

    result = await _run(_tool(handler), "http://example.com/start", tmp_path)
    assert result.is_error
    assert "Redirect (302)" in result.content
    assert "https://other.example/page" in result.content
    assert len(calls) == 1  # never followed


@pytest.mark.asyncio
async def test_oversized_response_rejected(no_dns, tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * (3 * 1024 * 1024))

    result = await _run(_tool(handler), "http://example.com/big", tmp_path)
    assert result.is_error
    assert "exceeded" in result.content


@pytest.mark.asyncio
async def test_truncation_above_50k_chars(no_dns, tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"y" * 60_000)

    result = await _run(_tool(handler), "http://example.com/long", tmp_path)
    assert not result.is_error
    assert len(result.content) == 50_000 + len("\n...[content truncated]")
    assert result.content.endswith("...[content truncated]")


@pytest.mark.asyncio
async def test_http_error_result(no_dns, tmp_path):
    def handler(request):
        raise httpx.ConnectError("refused")

    result = await _run(_tool(handler), "http://example.com/down", tmp_path)
    assert result.is_error


def test_needs_permissions_is_true():
    assert WebFetchTool().needs_permissions({"url": "http://example.com/"}) is True


@pytest.mark.asyncio
async def test_check_url_public_host_passes(no_dns):
    assert await check_url("http://example.com/") is None
