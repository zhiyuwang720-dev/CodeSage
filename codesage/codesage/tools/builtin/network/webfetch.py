"""WebFetch tool: fetch a URL, convert HTML to markdown, never follow
redirects, and refuse SSRF targets before any bytes are sent.

SSRF gates (checked before connecting, Kode semantics):
- scheme must be http/https, no embedded credentials (user:pass@)
- no bare hostnames (no dot, except localhost — which is then refused by
  the resolved-IP check anyway)
- every IP resolved by getaddrinfo must be public: private/loopback/
  link-local/reserved/unspecified ranges are refused (10/8, 172.16/12,
  192.168/16, 127/8, 169.254/16, 0.0.0.0, ::1, fc00::/7, fe80::/10,
  IPv4-mapped, ...)

# ponytail: DNS is checked once up front but httpx re-resolves on connect
# (TOCTOU on the resolved IP); pinning the connection to the checked IP is
# the upgrade if a DNS-rebinding threat model ever matters here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import urllib.parse
from html.parser import HTMLParser

import httpx

from ...base import Tool, ToolResult, ToolUseContext

FETCH_TIMEOUT_S = 30
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_CHARS = 50_000
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def _is_private_ip(ip: str) -> bool:
    """True when an IP must never be fetched (private/loopback/link-local/
    reserved/unspecified; IPv4-mapped v6 inherits the v4 verdict)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


async def check_url(url: str) -> str | None:
    """SSRF gate. Returns an error message to refuse the URL, else None."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "Error: invalid URL"
    if parsed.scheme not in ("http", "https"):
        return "Error: only http/https URLs are supported"
    if parsed.username or parsed.password:
        return "Error: URLs with embedded credentials are not allowed"
    hostname = parsed.hostname or ""
    if not hostname:
        return "Error: URL has no host"
    if "." not in hostname and hostname.lower() != "localhost":
        return "Error: bare hostnames are not allowed (host has no dots)"
    try:
        port = parsed.port or 80
    except ValueError:
        return "Error: invalid port in URL"
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo, hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM
            ),
            timeout=FETCH_TIMEOUT_S,
        )
    except (socket.gaierror, OSError, TimeoutError):
        return "Error: could not resolve host"
    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            return f"Error: host resolves to a private/non-routable address ({ip})"
    return None


class _HTMLToMarkdown(HTMLParser):
    """Minimal HTML->markdown: headings, paragraphs, links, lists, code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0  # script/style/noscript depth
        self._in_title = False
        self._link_href = ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag == "p":
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "pre":
            self.parts.append("\n```\n")
        elif tag == "a":
            self._link_href = dict(attrs).get("href", "")
            self.parts.append("[")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "pre":
            self.parts.append("\n```\n")
        elif tag == "a":
            self.parts.append(f"]({self._link_href})")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip()
            return
        self.parts.append(data)


def _html_to_markdown(raw: str) -> str:
    parser = _HTMLToMarkdown()
    try:
        parser.feed(raw)
    except Exception:
        pass  # malformed HTML: keep whatever parsed
    body = "".join(parser.parts)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return f"# {parser.title}\n\n{body}" if parser.title else body


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n...[content truncated]"


class WebFetchTool(Tool):
    name = "WebFetch"
    description = "Fetch a URL and return its content as markdown. Redirects are never followed — re-issue with the redirect target. Private/loopback addresses and credentialed URLs are refused."
    input_schema = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "http(s) URL to fetch"}},
        "required": ["url"],
    }
    is_concurrency_safe = True  # read-only network fetch

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        """Inject an httpx client (e.g. MockTransport) for tests."""
        self._http = http

    def needs_permissions(self, input: dict) -> bool:
        return True  # network access goes through the permission chain

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        url = str(input["url"]).strip()
        error = await check_url(url)
        if error:
            return ToolResult(error, is_error=True)

        client = self._http or httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S, follow_redirects=False
        )
        own_client = self._http is None
        try:
            async with client.stream("GET", url) as resp:
                if resp.status_code in REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    target = urllib.parse.urljoin(url, location) if location else "(no Location header)"
                    return ToolResult(
                        f"Redirect ({resp.status_code}) to {target}. Fetch the target URL directly.",
                        is_error=True,
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        return ToolResult(
                            f"Error: response exceeded {MAX_RESPONSE_BYTES} bytes", is_error=True
                        )
                    chunks.append(chunk)
                raw = b"".join(chunks).decode("utf-8", errors="replace")
        except httpx.TimeoutException:
            return ToolResult(f"Error: request timed out after {FETCH_TIMEOUT_S}s", is_error=True)
        except httpx.HTTPError as exc:
            return ToolResult(f"Error: request failed: {exc}", is_error=True)
        finally:
            if own_client:
                await client.aclose()

        content_type = resp.headers.get("content-type", "")
        text = _html_to_markdown(raw) if "text/html" in content_type.lower() else raw
        return ToolResult(_truncate(text))
