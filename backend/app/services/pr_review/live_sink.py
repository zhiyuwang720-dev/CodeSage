"""runtime 引擎三屏流式 TUI(--live)。

把三个审查视角(security / architecture / quality)的运行时事件流转成终端三屏:
每屏 = 一个视角 Agent 的滚动活动区(流式正文原地增长 + 工具调用/重试/错误追加行),
顶部一行项目信息与各视角 sessionID。

设计:
- 纯 ANSI + stdlib(unicodedata 做 CJK 显示宽度), 不引入终端库依赖。
- 事件契约同 RuntimeProgressSink(query_loop._emit_event): 同步内层, 返回 None。
- 状态由 async 事件循环(__call__)更新, 后台线程 ~10fps 读快照画 stderr;
  stdout 始终保持纯净 JSON(基准 harness 从 stdout 解析)。
- render_frame 是纯函数(输入 panes/meta/elapsed/尺寸 → 行列表), 便于单测。
"""
from __future__ import annotations

import shutil
import sys
import threading
import time
import unicodedata
from typing import Any, TextIO

PERSPECTIVES: tuple[str, ...] = ("security", "architecture", "quality")

_STATUS_SYMBOL: dict[str, str] = {
    "waiting": "·",
    "thinking": "…",
    "streaming": "▸",
    "retrying": "↻",
    "error": "✗",
    "done": "✓",
}


def display_width(text: str) -> int:
    """终端显示宽度: CJK 宽字符(W/F)记 2, 其余(盒线/西文/标点)记 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad_right(text: str, width: int) -> str:
    gap = width - display_width(text)
    return text if gap <= 0 else text + " " * gap


def _truncate(text: str, width: int) -> str:
    """按显示宽度截断, 末尾补 …(保留至少 1 格给省略号)。"""
    if display_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        ch_w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + ch_w > max(0, width - 1):
            break
        out.append(ch)
        used += ch_w
    return "".join(out) + "…"


def _wrap_text(text: str, width: int) -> list[str]:
    """按显示宽度把一行拆成 <=width 的网格行(满宽硬换行)。"""
    if width <= 0:
        return [""]
    rows: list[str] = []
    current: list[str] = []
    used = 0
    for ch in text:
        ch_w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + ch_w > width:
            rows.append("".join(current))
            current = [ch]
            used = ch_w
        else:
            current.append(ch)
            used += ch_w
    if current:
        rows.append("".join(current))
    return rows or [""]


def _format_elapsed(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class _PerspectiveState:
    """单视角的累计状态(事件循环写入, 渲染线程读快照)。"""

    def __init__(self, perspective: str) -> None:
        self.perspective = perspective
        self.status: str = "waiting"  # waiting|thinking|streaming|retrying|error|done
        self.turn_count = 0
        self.findings = 0
        self.session_id: str | None = None
        self.retry: tuple[Any, Any, Any] | None = None
        self.last_error: str | None = None
        self.current_text = ""  # 当前回合流式正文(原地增长)
        self.activity: list[str] = []  # 已闭合行(工具调用/重试/回合结束…)

    def snapshot(self) -> dict[str, Any]:
        return {
            "perspective": self.perspective,
            "status": self.status,
            "turn_count": self.turn_count,
            "findings": self.findings,
            "session_id": self.session_id,
            "retry": self.retry,
            "last_error": self.last_error,
            "current_text": self.current_text,
            "activity": list(self.activity),
        }


def _pane_lines(pane: dict[str, Any], col_w: int, body_h: int) -> list[str]:
    """单屏渲染: 标题行 + body_h 行滚动活动区(底部对齐)。"""
    symbol = _STATUS_SYMBOL.get(str(pane.get("status") or "waiting"), "·")
    title_bits = [f"{symbol} {pane.get('perspective', '?')}"]
    if pane.get("turn_count"):
        title_bits.append(f"第{pane['turn_count']}轮")
    if pane.get("findings"):
        title_bits.append(f"{pane['findings']}发现")
    retry = pane.get("retry")
    if retry:
        attempt, max_attempts, error_type = retry
        label = f"重试{attempt}/{max_attempts}"
        if error_type:
            label += f" {error_type}"
        title_bits.append(label)
    lines = [_pad_right(_truncate(" ".join(title_bits), col_w), col_w)]

    body_text = list(pane.get("activity") or [])
    current = str(pane.get("current_text") or "").strip()
    if current:
        body_text.append(current)
    wrapped: list[str] = []
    for line in body_text:
        wrapped.extend(_wrap_text(line, col_w))
    # 标题占第 1 行, 正文占剩余 body_h-1 行(底部对齐, 取最新)
    for line in wrapped[-(body_h - 1):] if body_h > 1 else []:
        lines.append(_pad_right(_truncate(line, col_w), col_w))
    while len(lines) < body_h:
        lines.append(" " * col_w)
    return lines[:body_h]


def render_frame(
    panes: dict[str, dict[str, Any]],
    meta: dict[str, Any],
    elapsed: float,
    width: int = 80,
    height: int = 24,
) -> list[str]:
    """把快照渲染成终端帧(纯函数, 无 ANSI 转义, 便于单测)。"""
    width = max(12, int(width))
    height = max(5, int(height))
    col_w = max(4, (width - 4) // 3)
    body_h = max(1, height - 4)
    ordered = list(panes.values())[:3]

    def border(left: str, mid: str, right: str) -> str:
        seg = "─" * col_w
        return left + seg + mid + seg + mid + seg + right

    line0 = "┌" + _pad_right(_truncate(" CodeSage runtime ", width - 2), width - 2) + "┐"

    bits: list[str] = []
    if meta.get("repo"):
        repo = str(meta["repo"])
        pr = meta.get("pr_number")
        bits.append(f"项目: {repo}" + (f"#{pr}" if pr else ""))
    if meta.get("model"):
        bits.append(f"模型: {meta['model']}")
    if meta.get("engine"):
        bits.append(f"引擎: {meta['engine']}")
    bits.append(_format_elapsed(elapsed))
    session_bits: list[str] = []
    for pane in ordered:
        sid = str(pane.get("session_id") or "")
        session_bits.append(f"{pane.get('perspective', '?')}={sid[:8] + '…' if len(sid) > 8 else (sid or '…')}")
    if session_bits:
        bits.append("会话 " + " ".join(session_bits))
    line1 = "│" + _pad_right(_truncate(" " + " · ".join(bits) + " ", width - 2), width - 2) + "│"

    body_rows: list[str] = []
    for row in range(body_h):
        cells: list[str] = []
        for pane in ordered:
            pane_lines = _pane_lines(pane, col_w, body_h)
            cells.append(pane_lines[row] if row < len(pane_lines) else " " * col_w)
        body_rows.append("│" + "│".join(cells) + "│")

    return [line0, line1, border("├", "┼", "┤")] + body_rows + [border("└", "┴", "┘")]


class LiveReviewSink:
    """把 runtime 事件流渲染成三屏 TUI(同步 event_sink, 返回 None)。"""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        enabled: bool = True,
        refresh_hz: float = 10.0,
        start_thread: bool | None = None,
    ):
        self._stream = stream or sys.stderr
        self._enabled = enabled
        self._refresh_hz = max(1.0, refresh_hz)
        self._start = time.monotonic()
        self._lock = threading.RLock()
        self._meta: dict[str, Any] = {}
        self._panes: dict[str, _PerspectiveState] = {
            name: _PerspectiveState(name) for name in PERSPECTIVES
        }
        self._closed = False
        self._render_thread: threading.Thread | None = None
        if start_thread is None:
            start_thread = enabled and self._isatty()
        if start_thread:
            self._start_render_thread()

    def _isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def _perspective(self, event: dict[str, Any]) -> _PerspectiveState:
        name = str(event.get("perspective") or "?")
        pane = self._panes.get(name)
        if pane is None:
            pane = _PerspectiveState(name)
            self._panes[name] = pane
        return pane

    def __call__(self, event: dict[str, Any]) -> None:
        """同步事件入口: 返回 None, 兼容 query_loop 的 isawaitable 检查。"""
        if not self._enabled:
            return None
        event_type = str(event.get("type") or "").strip()
        with self._lock:
            if event_type == "meta":
                for key in ("project_id", "repo", "pr_number", "engine", "model"):
                    value = event.get(key)
                    if value is not None:
                        self._meta[key] = value
            elif event_type == "session_start":
                pane = self._perspective(event)
                pane.session_id = str(event.get("session_id") or "")
            elif event_type == "perspective_start":
                pane = self._perspective(event)
                pane.status = "thinking"
            elif event_type == "assistant_start":
                pane = self._perspective(event)
                pane.turn_count += 1
                pane.retry = None
                pane.status = "streaming"
            elif event_type == "token":
                pane = self._perspective(event)
                pane.current_text += str(event.get("content") or "")
                pane.status = "streaming"
            elif event_type == "reasoning_delta":
                # 推理增量只在状态行体现"思考中", 不混入正文流
                pane = self._perspective(event)
                pane.status = "streaming"
            elif event_type == "tool_call":
                pane = self._perspective(event)
                self._close_line(pane)
                tool = event.get("tool_call") or {}
                pane.activity.append(f"⚙ {tool.get('name') or '?'}")
            elif event_type == "done":
                pane = self._perspective(event)
                self._close_line(pane)
                pane.status = "thinking"
            elif event_type == "llm_retry":
                pane = self._perspective(event)
                self._close_line(pane)
                pane.retry = (
                    event.get("attempt"),
                    event.get("max_attempts"),
                    event.get("error_type"),
                )
                label = f"↻ 重试 {event.get('attempt')}/{event.get('max_attempts')}"
                if event.get("error_type"):
                    label += f": {event['error_type']}"
                pane.activity.append(label)
                pane.status = "retrying"
            elif event_type == "error":
                pane = self._perspective(event)
                self._close_line(pane)
                message = (
                    event.get("message_text")
                    or event.get("message")
                    or event.get("error_type")
                    or "未知错误"
                )
                pane.last_error = str(message)
                pane.activity.append(f"✗ {message}")
                pane.status = "error"
            elif event_type == "perspective_done":
                pane = self._perspective(event)
                self._close_line(pane)
                pane.findings = int(event.get("findings") or 0)
                turn_count = event.get("turn_count")
                if turn_count is not None:
                    pane.turn_count = int(turn_count)
                pane.status = "done"
                pane.activity.append(f"✓ 视角完成: {pane.turn_count} 轮, {pane.findings} 发现")
        return None

    @staticmethod
    def _close_line(pane: _PerspectiveState) -> None:
        text = (pane.current_text or "").strip()
        if text:
            pane.activity.append(text)
        pane.current_text = ""

    # ── 渲染线程 ──

    def _start_render_thread(self) -> None:
        self._render_thread = threading.Thread(
            target=self._render_loop, name="codesage-live-sink", daemon=True
        )
        self._render_thread.start()

    def _render_loop(self) -> None:
        stream = self._stream
        try:
            stream.write("\x1b[?25l")  # 隐藏光标, 避免刷屏期间闪烁
            stream.flush()
        except Exception:
            return
        try:
            while not self._closed:
                self._redraw()
                time.sleep(1.0 / self._refresh_hz)
        except Exception:
            pass
        finally:
            try:
                stream.write("\x1b[?25h")  # 恢复光标
                stream.flush()
            except Exception:
                pass

    def _redraw(self) -> None:
        try:
            width, height = shutil.get_terminal_size(fallback=(80, 24))
        except Exception:
            width, height = 80, 24
        with self._lock:
            panes = {name: pane.snapshot() for name, pane in self._panes.items()}
            meta = dict(self._meta)
            elapsed = time.monotonic() - self._start
        # 保留最后一行给光标, 避免每帧触发终端滚动(抖动)
        frame = render_frame(panes, meta, elapsed, width, max(4, height - 1))
        try:
            self._stream.write("\x1b[H" + "\n".join(line + "\x1b[K" for line in frame) + "\x1b[J")
            self._stream.flush()
        except Exception:
            pass

    def close(self) -> None:
        self._closed = True
        if self._render_thread is not None:
            self._render_thread.join(timeout=1.0)
        try:
            # 光标位于 TUI 末行下方: 清掉残余后换行, 让 stdout 的 JSON 落在 TUI 下方
            self._stream.write("\x1b[J\n")
            self._stream.flush()
        except Exception:
            pass
