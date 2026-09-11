"""真实进程外 HTTP/SSE fixture：验证 SDK 实际发出的请求（禁止 mock SDK 发送）。

服务器是 stdlib `ThreadingHTTPServer`，与 SDK 客户端处于同一进程但独立线程；
除 HTTP 之外没有共享状态，因此「请求次数」等证据来自服务器真实收到的报文。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional


@dataclass
class HttpRequestRecord:
    path: str
    method: str
    headers: Dict[str, str]
    body: Dict[str, Any]

    @property
    def stream_requested(self) -> bool:
        return bool(self.body.get("stream"))


@dataclass
class RouteScript:
    """一个端点路径的应答脚本。"""

    # 每次命中依次消费；耗尽后重复最后一项。
    responses: List["PlannedResponse"] = field(default_factory=list)
    # 按请求序号命中：index -> PlannedResponse（覆盖 responses）
    by_index: Dict[int, "PlannedResponse"] = field(default_factory=dict)


@dataclass
class PlannedResponse:
    status: int = 200
    payload: Optional[Dict[str, Any]] = None
    sse_chunks: Optional[List[Dict[str, Any]]] = None
    sse_raw_lines: Optional[List[str]] = None
    sse_done: bool = True
    delay_before_seconds: float = 0.0
    delay_between_chunks_seconds: float = 0.0
    truncate_after_chunks: Optional[int] = None
    # 等价于 truncate_after_chunks，但语义是「模拟网络中断」；两者共用截断实现。
    fail_mid_stream_after_chunks: Optional[int] = None
    streaming: bool = False
    content_type: str = "application/json"
    headers: Dict[str, str] = field(default_factory=dict)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CodeSageModelFixture/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 基类签名
        return

    def do_POST(self) -> None:  # noqa: N802 - 基类签名
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            body = {"__raw__": raw.decode("utf-8", errors="replace")}
        record = HttpRequestRecord(
            path=self.path,
            method="POST",
            headers={key.lower(): value for key, value in self.headers.items()},
            body=body,
        )
        planned = self.server.fixture.plan(self.path, record)  # type: ignore[attr-defined]
        self.server.records.append(record)  # type: ignore[attr-defined]
        self._respond(planned, record)

    def do_GET(self) -> None:  # noqa: N802 - 基类签名
        planned = self.server.fixture.plan(self.path, None)  # type: ignore[attr-defined]
        self._respond(planned, None)

    def _respond(self, planned: PlannedResponse, record: Optional[HttpRequestRecord]) -> None:
        if planned.sse_raw_lines is not None or planned.sse_chunks is not None:
            try:
                self._write_sse(planned)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if planned.delay_before_seconds:
            import time

            time.sleep(planned.delay_before_seconds)

        payload = planned.payload if planned.payload is not None else {}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(planned.status)
        self.send_header("Content-Type", planned.content_type)
        self.send_header("Content-Length", str(len(encoded)))
        for key, value in planned.headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)
        self.wfile.flush()

    def _write_sse(self, planned: PlannedResponse) -> None:
        """把 SSE 事件按计划写出。

        默认用 `Content-Length` 一次性写出（与真实 SSE 语义一致、客户端解析无歧义）；
        `streaming=True` 时改用 HTTP/1.1 chunked 逐事件推送，用于挂起/取消场景。
        """

        events: List[str] = []
        if planned.sse_raw_lines is not None:
            events = _group_sse_events(planned.sse_raw_lines)
        else:
            for chunk in planned.sse_chunks or []:
                events.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
        limit = (
            planned.truncate_after_chunks
            if planned.truncate_after_chunks is not None
            else planned.fail_mid_stream_after_chunks
        )
        if planned.sse_done and limit is None:
            events.append("data: [DONE]\n\n")

        if limit is not None:
            events = events[:limit]

        if not planned.streaming:
            body = "".join(events).encode("utf-8")
            self.send_response(planned.status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            for key, value in planned.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if planned.delay_before_seconds:
                # 先发响应头再延迟：客户端能观察到首个事件迟到。
                import time

                time.sleep(planned.delay_before_seconds)
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            if limit is not None:
                self.close_connection = True
            return

        self.send_response(planned.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        if limit is None:
            # 正常结束时显式声明 close（客户端据此知道流已收尾）；
            # 中断场景保持长连接语义，由半截 chunk 触发客户端读错误。
            self.send_header("Connection", "close")
        for key, value in planned.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if planned.delay_before_seconds:
            # 响应头已发出，但首个事件延迟到达：用于验证首事件超时。
            import time

            time.sleep(planned.delay_before_seconds)
        try:
            for event in events:
                self._write_chunked(event.encode("utf-8"))
                if planned.delay_between_chunks_seconds:
                    import time

                    time.sleep(planned.delay_between_chunks_seconds)
            if limit is not None:
                # 不发终止块直接断开，模拟真实流中断。
                self._close_abruptly()
                return
            self._write_chunked(b"")
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _write_chunked(self, payload: bytes) -> None:
        try:
            if not payload:
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.wfile.write(f"{len(payload):X}\r\n".encode("ascii") + payload + b"\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _close_abruptly(self) -> None:
        try:
            self.wfile.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.connection.close()
        except Exception:  # noqa: BLE001
            pass


class FixtureServer:
    """本地模型端点。`base_url` 直接可给 SDK 当 api_base。"""

    def __init__(self) -> None:
        self.records: List[HttpRequestRecord] = []
        self._routes: Dict[str, RouteScript] = {}
        self._defaults: Dict[str, PlannedResponse] = {}
        self._hits: Dict[str, int] = {}
        self._hooks: List[Callable[[HttpRequestRecord], None]] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fixture = self  # type: ignore[attr-defined]
        self._server.records = self.records  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # ---- 生命周期
    def start(self) -> "FixtureServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def root_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ---- 脚本
    def route(self, path: str, script: RouteScript) -> None:
        self._routes[path] = script

    def set_default(self, path: str, response: PlannedResponse) -> None:
        self._defaults[path] = response

    def on_request(self, hook: Callable[[HttpRequestRecord], None]) -> None:
        self._hooks.append(hook)

    def plan(self, path: str, record: Optional[HttpRequestRecord]) -> PlannedResponse:
        with self._lock:
            index = self._hits.get(path, 0)
            self._hits[path] = index + 1
        if record is not None:
            for hook in list(self._hooks):
                hook(record)
        script = self._routes.get(path) or self._routes.get("*")
        if script is not None:
            if index in script.by_index:
                return script.by_index[index]
            if script.responses:
                position = min(index, len(script.responses) - 1)
                return script.responses[position]
        fallback = self._defaults.get(path) or self._defaults.get("*")
        if fallback is not None:
            return fallback
        return PlannedResponse(status=404, payload={"error": {"message": f"no fixture route for {path}"}})

    # ---- 证据
    def requests(self, path: Optional[str] = None) -> List[HttpRequestRecord]:
        with self._lock:
            items = list(self.records)
        if path is None:
            return items
        return [item for item in items if item.path == path]

    def request_count(self, path: str) -> int:
        return len(self.requests(path))

    def reset(self) -> None:
        with self._lock:
            self.records.clear()
            self._hits.clear()


# ------------------------------------------------------------------ 应答构造器

def openai_completion(
    *,
    content: str = "hello",
    model: str = "fixture-model",
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
    usage_style: str = "openai",
    finish_reason: str = "stop",
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    extra_usage: Optional[Dict[str, Any]] = None,
    include_usage: bool = True,
) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": item.get("id") or f"call_{index}",
                "type": "function",
                "function": {
                    "name": item["name"],
                    "arguments": item.get("arguments") or "{}",
                },
            }
            for index, item in enumerate(tool_calls)
        ]
    payload: Dict[str, Any] = {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if include_usage:
        payload["usage"] = build_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            style=usage_style,
            extra_usage=extra_usage,
        )
    return payload


def build_usage(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    style: str = "openai",
    extra_usage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if style in {"openai", "openai_chat"}:
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    elif style == "anthropic":
        usage = {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
        }
    elif style == "gemini":
        usage = {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": completion_tokens,
            "totalTokenCount": prompt_tokens + completion_tokens,
        }
    else:
        raise ValueError(f"unknown usage style: {style}")
    if extra_usage:
        usage.update(extra_usage)
    return usage


def openai_stream_chunks(
    *,
    content: str = "hello world",
    model: str = "fixture-model",
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
    usage_style: str = "openai",
    reasoning: Optional[str] = None,
    tool_call_chunks: Optional[List[Dict[str, Any]]] = None,
    finish_reason: str = "stop",
    include_usage_chunk: bool = True,
    usage_zero: bool = False,
    empty_chunks: int = 0,
) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []

    def delta_chunk(delta: Dict[str, Any], finish: Optional[str] = None) -> Dict[str, Any]:
        return {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    for _ in range(empty_chunks):
        chunks.append(delta_chunk({}))

    if reasoning:
        for piece in _split(reasoning, 8):
            chunks.append(delta_chunk({"reasoning_content": piece}))

    for piece in _split(content, 5):
        chunks.append(delta_chunk({"content": piece}))

    if tool_call_chunks:
        for item in tool_call_chunks:
            chunk = delta_chunk({"tool_calls": [item]})
            chunks.append(chunk)

    if include_usage_chunk:
        usage_payload = build_usage(
            prompt_tokens=0 if usage_zero else prompt_tokens,
            completion_tokens=0 if usage_zero else completion_tokens,
            style=usage_style,
        )
        chunks.append(
            {
                "id": "chatcmpl-fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": model,
                "choices": [],
                "usage": usage_payload,
            }
        )

    chunks.append(delta_chunk({}, finish=finish_reason))
    return chunks


def tool_call_delta(
    *,
    index: int = 0,
    call_id: Optional[str] = None,
    name: Optional[str] = None,
    arguments: Optional[str] = None,
) -> Dict[str, Any]:
    function: Dict[str, Any] = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    payload: Dict[str, Any] = {"index": index, "function": function}
    if call_id is not None:
        payload["id"] = call_id
        payload["type"] = "function"
    return payload


def anthropic_message(
    *,
    content: str = "hello",
    model: str = "claude-fixture",
    input_tokens: int = 11,
    output_tokens: int = 7,
    cache_read_input_tokens: Optional[int] = None,
    cache_creation_input_tokens: Optional[int] = None,
    stop_reason: str = "end_turn",
    tool_uses: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    blocks: List[Dict[str, Any]] = []
    if content:
        blocks.append({"type": "text", "text": content})
    for index, item in enumerate(tool_uses or []):
        blocks.append(
            {
                "type": "tool_use",
                "id": item.get("id") or f"toolu_{index}",
                "name": item["name"],
                "input": item.get("input") or {},
            }
        )
    usage: Dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if cache_read_input_tokens is not None:
        usage["cache_read_input_tokens"] = cache_read_input_tokens
    if cache_creation_input_tokens is not None:
        usage["cache_creation_input_tokens"] = cache_creation_input_tokens
    return {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


def anthropic_stream_lines(
    *,
    content: str = "hello world",
    model: str = "claude-fixture",
    input_tokens: int = 11,
    output_tokens: int = 7,
    tool_uses: Optional[List[Dict[str, Any]]] = None,
    stop_reason: str = "end_turn",
) -> List[str]:
    """Anthropic Messages SSE 原始行（含 `event:` 名与 `data:` JSON）。"""

    events: List[Dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    ]
    for piece in _split(content, 4):
        events.append(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": piece},
            }
        )
    events.append({"type": "content_block_stop", "index": 0})

    next_index = 1
    for item in tool_uses or []:
        events.append(
            {
                "type": "content_block_start",
                "index": next_index,
                "content_block": {
                    "type": "tool_use",
                    "id": item.get("id") or f"toolu_{next_index}",
                    "name": item["name"],
                    "input": {},
                },
            }
        )
        partial = json.dumps(item.get("input") or {}, ensure_ascii=False)
        for piece in _split(partial, 4):
            events.append(
                {
                    "type": "content_block_delta",
                    "index": next_index,
                    "delta": {"type": "input_json_delta", "partial_json": piece},
                }
            )
        events.append({"type": "content_block_stop", "index": next_index})
        next_index += 1

    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        }
    )
    events.append({"type": "message_stop"})
    combined: List[str] = []
    for event in events:
        combined.append(f"event: {event['type']}")
        combined.append(json.dumps(event, ensure_ascii=False))
        combined.append("")
    return combined


def _split(text: str, size: int) -> List[str]:
    return [text[index:index + size] for index in range(0, len(text), size)] or [""]


def _group_sse_events(lines: List[str]) -> List[str]:
    """把「event 名 + JSON」行序列合成完整 SSE 事件字符串（自动补 `data:` 前缀）。"""

    events: List[str] = []
    buffer: List[str] = []
    for line in lines:
        if line == "":
            if buffer:
                events.append("\n".join(buffer) + "\n\n")
                buffer = []
            continue
        if line.startswith(("event:", "data:", "id:", "retry:", ":")):
            buffer.append(line)
        else:
            buffer.append(f"data: {line}")
    if buffer:
        events.append("\n".join(buffer) + "\n\n")
    return events
