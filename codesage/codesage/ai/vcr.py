"""VCR: record/replay LLM HTTP calls at the transport layer.

Fingerprint = sha1(method + url + body). Fixtures live under CODESAGE_VCR_DIR
(default .vcr/). Modes: off (default), record (forward + save), replay
(serve fixtures; a miss raises so CI catches drift).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx


class VCRTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        mode: str | None = None,
        vcr_dir: Path | None = None,
        inner: httpx.AsyncBaseTransport | None = None,
    ):
        self.mode = mode or os.getenv("CODESAGE_VCR", "off")
        self.dir = vcr_dir or Path(os.getenv("CODESAGE_VCR_DIR", ".vcr"))
        self._inner = inner

    def _fingerprint(self, request: httpx.Request) -> str:
        payload = {
            "method": request.method,
            "url": str(request.url),
            "body": request.content.decode("utf-8", "replace"),
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        fp = self._fingerprint(request)
        fixture = self.dir / f"{fp}.json"
        if self.mode == "replay":
            if not fixture.exists():
                raise RuntimeError(f"VCR replay miss for {request.method} {request.url} ({fp})")
            data = json.loads(fixture.read_text(encoding="utf-8"))
            return httpx.Response(
                status_code=data["status"], content=data["body"].encode(), request=request
            )
        # off / record: forward to inner (real network by default, injectable for tests)
        if self._inner is None:
            self._inner = httpx.AsyncHTTPTransport()
        response = await self._inner.handle_async_request(request)
        if self.mode == "record" and response.status_code < 400:
            self.dir.mkdir(parents=True, exist_ok=True)
            fixture.write_text(
                json.dumps({"status": response.status_code, "body": response.text}, ensure_ascii=False),
                encoding="utf-8",
            )
        return response
