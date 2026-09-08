"""runtime 引擎 CLI 进度输出(阶段 02 补 CLI 可观测性)。

CLI 的 runtime 路径原本全程静默: 三视角 LLM 编排(每视角多轮非流式调用)跑完后
才在最后一次性 print JSON(cli.py), 端点不稳或重试时会长时间看不到任何反馈。
此模块把运行时引擎已存在但被丢弃的事件流(app/services/runtime/query_loop._emit_event 的
assistant_start / done / llm_retry / error)转成 stderr 上的粗粒度进度行。

设计:
- 只打视角/回合/重试/错误级别的信号, 不打 token 内容(防刷屏与内容泄漏)。
- 每行带自 sink 创建起的耗时前缀, 直接回答"已经跑了多久"。
- 进度打到 stderr(flush=True), stdout 保持纯净 JSON(基准 harness 从 stdout 解析)。
- enabled=False 时为空操作, 不改变既有行为。

事件契约与产品 API 路径一致(audit_sessions.collect_event):
event_sink = Callable[[dict], Any], 同步或异步均可(query_loop._emit_event 会
isawaitable 判断)。
"""
from __future__ import annotations

import sys
import time
from typing import Any, TextIO

# 这些事件只关心"发生与否"的信号, 忽略其携带的正文/消息内容。
_SIGNAL_EVENT_TYPES = {
    "perspective_start",
    "perspective_done",
    "assistant_start",
    "done",
    "llm_retry",
    "error",
}


class RuntimeProgressSink:
    """把 runtime 引擎的内部事件流渲染成 stderr 进度行。"""

    def __init__(self, stream: TextIO | None = None, *, enabled: bool = True):
        self._stream = stream or sys.stderr
        self._enabled = enabled
        self._start = time.monotonic()
        # perspective -> 已开始的 LLM 回合数
        self._turns: dict[str, int] = {}

    def __call__(self, event: dict[str, Any]) -> None:
        """同步 event_sink 入口(返回 None, 兼容 query_loop 的 isawaitable 检查)。"""
        if not self._enabled:
            return None
        event_type = str(event.get("type") or "").strip()
        if event_type not in _SIGNAL_EVENT_TYPES:
            return None
        perspective = str(event.get("perspective") or "?")
        elapsed = time.monotonic() - self._start
        if event_type == "perspective_start":
            self._write(elapsed, perspective, "视角开始")
        elif event_type == "perspective_done":
            self._write(
                elapsed,
                perspective,
                f"视角完成: {event.get('turn_count', '?')} 轮, {event.get('findings', 0)} 条发现",
            )
        elif event_type == "assistant_start":
            self._turns[perspective] = self._turns.get(perspective, 0) + 1
            self._write(elapsed, perspective, f"第 {self._turns[perspective]} 轮 LLM 调用…")
        elif event_type == "done":
            self._write(elapsed, perspective, "本轮模型响应完成")
        elif event_type == "llm_retry":
            self._write(
                elapsed,
                perspective,
                f"LLM 重试 {event.get('attempt', '?')}/{event.get('max_attempts', '?')}: "
                f"{event.get('error_type') or event.get('message_text') or '网络错误'}",
            )
        elif event_type == "error":
            self._write(
                elapsed,
                perspective,
                f"错误: {event.get('message_text') or event.get('message') or event.get('error') or '未知错误'}",
            )
        return None

    def _write(self, elapsed: float, perspective: str, message: str) -> None:
        self._stream.write(f"[runtime {elapsed:6.1f}s] {perspective}: {message}\n")
        self._stream.flush()

    def close(self) -> None:
        """统一 sink 生命周期接口(cli 在收尾时统一调用); 行模式无后台线程, 空操作。"""
        return None
