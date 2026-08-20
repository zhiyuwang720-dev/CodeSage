"""Single-shot mode tests (mock LLM, offline)."""

import asyncio
import io

import pytest

from codesage.ai import StreamEvent
from codesage.cli.assemble import build_loop
from codesage.cli.repl import graceful_shutdown, run_single_turn
from codesage.config import paths


class MockLLM:
    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.total_cost = [0.0]

    def stream(self, request, model="main"):
        return self._gen()

    async def _gen(self):
        events = self.script[min(self.calls, len(self.script) - 1)](self.calls)
        self.calls += 1
        for ev in events:
            yield ev


def _mock_loop(tmp_path, script, mode="default", monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / ".codesage")
    loop = build_loop(cwd=tmp_path, mode=mode)
    loop.client = MockLLM(script)  # replace real client
    return loop


async def _run(loop, user_input, **kw):
    buf = io.StringIO()
    await run_single_turn(loop, user_input, out=buf, **kw)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_single_turn_end_to_end(tmp_path, monkeypatch):
    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="hello"), StreamEvent(type="done", stop_reason="end_turn")]],
        monkeypatch=monkeypatch,
    )
    out = await _run(loop, "hi")
    assert "hello" in out


@pytest.mark.asyncio
async def test_single_turn_tool_flow(tmp_path, monkeypatch):
    """tool_use → permission denied (no UI in single-shot) → model sees denial."""
    script = [
        lambda i: [
            StreamEvent(type="tool_use_start", tool_use_id="t1", tool_name="Bash"),
            StreamEvent(type="tool_use_delta", input_json_delta='{"command": "ls"}'),
            StreamEvent(type="done", stop_reason="tool_use"),
        ],
        lambda i: [StreamEvent(type="text_delta", text="denied, moving on"), StreamEvent(type="done")],
    ]
    loop = _mock_loop(tmp_path, script, monkeypatch=monkeypatch)
    out = await _run(loop, "run ls")
    assert "denied, moving on" in out
    assert "✗" in out  # denial tool_result rendered


@pytest.mark.asyncio
async def test_session_persisted(tmp_path, monkeypatch):
    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="ok"), StreamEvent(type="done")]],
        monkeypatch=monkeypatch,
    )
    await _run(loop, "hi")
    assert loop.session.path.exists()
    assert len(loop.session.load()) == 2  # isolated per test: user + assistant


@pytest.mark.asyncio
async def test_mode_yolo_still_requires_explicit_approval(tmp_path, monkeypatch):
    """Bash needs explicit approval even in yolo; single-shot has no UI → denied."""
    script = [
        lambda i: [
            StreamEvent(type="tool_use_start", tool_use_id="t1", tool_name="Bash"),
            StreamEvent(type="tool_use_delta", input_json_delta='{"command": "echo hi"}'),
            StreamEvent(type="done", stop_reason="tool_use"),
        ],
        lambda i: [StreamEvent(type="text_delta", text="ran it"), StreamEvent(type="done")],
    ]
    loop = _mock_loop(tmp_path, script, mode="yolo", monkeypatch=monkeypatch)
    out = await _run(loop, "run echo")
    assert "Permission denied" in out


# ---- CC-10: structured stop reason (AgentLoop.last_stop_reason) ----

@pytest.mark.asyncio
async def test_stop_reason_max_budget_sets_flag(tmp_path, monkeypatch):
    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="all done"), StreamEvent(type="done")]],
        monkeypatch=monkeypatch,
    )
    loop.max_budget_usd = 0.0  # client cost starts at 0.0 → budget hit at loop top
    s = await run_single_turn(loop, "hi", render=False)
    assert loop.last_stop_reason == "max_budget"
    assert s.budget_exceeded is True
    assert s.max_turns_exceeded is False


@pytest.mark.asyncio
async def test_stop_reason_max_turns_sets_flag(tmp_path, monkeypatch):
    loop = _mock_loop(
        tmp_path,
        [lambda i: [
            StreamEvent(type="tool_use_start", tool_use_id="t1", tool_name="Bash"),
            StreamEvent(type="tool_use_delta", input_json_delta='{"command": "ls"}'),
            StreamEvent(type="done", stop_reason="tool_use"),
        ]],
        monkeypatch=monkeypatch,
    )
    loop.max_turns = 1  # turn 0 runs; the loop-top check after it stops the loop
    s = await run_single_turn(loop, "hi", render=False)
    assert loop.last_stop_reason == "max_turns"
    assert s.max_turns_exceeded is True
    assert s.budget_exceeded is False


@pytest.mark.asyncio
async def test_stop_reason_completed_no_flags(tmp_path, monkeypatch):
    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="finished"), StreamEvent(type="done")]],
        monkeypatch=monkeypatch,
    )
    s = await run_single_turn(loop, "hi", render=False)
    assert loop.last_stop_reason == "completed"
    assert s.budget_exceeded is False
    assert s.max_turns_exceeded is False


@pytest.mark.asyncio
async def test_no_stop_reason_falls_back_to_text_sniff():
    """Loop without last_stop_reason (pre-landing/foreign fakes): legacy sniff."""
    from codesage.core import assistant_message

    class PlainLoop:
        client = None
        session = None
        max_budget_usd = None

        async def run(self, user_input):
            yield assistant_message("Stopped: maximum budget reached.")

    s = await run_single_turn(PlainLoop(), "hi", render=False)
    assert s.budget_exceeded is True


async def test_tool_start_status_line(tmp_path, monkeypatch):
    """PI-01 wiring: REPL prints a status line when a tool starts running."""
    from codesage.tools.builtin.filesystem.ls import LSTool

    script = [
        lambda i: [
            StreamEvent(type="tool_use_start", tool_use_id="t1", tool_name="LS"),
            StreamEvent(type="tool_use_delta", input_json_delta='{"path": "."}'),
            StreamEvent(type="done", stop_reason="tool_use"),
        ],
        lambda i: [StreamEvent(type="text_delta", text="done"), StreamEvent(type="done")],
    ]
    loop = _mock_loop(tmp_path, script, monkeypatch=monkeypatch)
    buf = io.StringIO()
    await run_single_turn(loop, "list", out=buf)
    assert "LS running" in buf.getvalue()


async def test_on_after_render_hook_fires(tmp_path):
    """The status-bar redraw hook fires after rendered output (streamed
    deltas, tool events, messages) — and never when render is off."""
    def text_events(i):
        return [
            StreamEvent(type="text_delta", text="hello"),
            StreamEvent(type="done", stop_reason="end_turn"),
        ]

    fires = []

    async def run(render):
        fires.clear()
        loop = _mock_loop(tmp_path, [text_events])
        await run_single_turn(
            loop, "hi", out=io.StringIO(), render=render,
            on_after_render=lambda: fires.append(1),
        )

    # rendered: one fire per rendered message (streamed deltas are buffered
    # into the final message render — see test_word_granular_deltas_no_stray_punct)
    await run(True)
    assert len(fires) >= 1
    # render off (--output-format json): nothing fires
    await run(False)
    assert fires == []


@pytest.mark.asyncio
async def test_word_granular_deltas_no_stray_punct(tmp_path, monkeypatch):
    """回归:DeepSeek 词级流式 chunk("标点+\\n" 单独成 chunk)不得在正文前
    渲染出孤立标点行。正文由 render_message 全量渲染,折叠行保持在正文前。"""
    body = "It looks like your message got cut off — you just sent \"sd\". What would you like me to do? For example:\n- Work on something in the `feat/10-compact` branch (e.g., engine compaction features)\n"
    # 按 DeepSeek 实际粒度切分:词与行尾标点各自成 chunk
    chunks = ["It looks like your message got cut off — you just sent ", '"sd"', ". What would you like me to do? For example", ":\n", "- Work on something in the `feat/10-compact` branch (e.g., engine compaction features", ")\n"]
    assert "".join(chunks) == body
    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text=c) for c in chunks]
         + [StreamEvent(type="done", stop_reason="end_turn")]],
        monkeypatch=monkeypatch,
    )
    out = await _run(loop, "hi")
    # 正文完整(render_message 全量渲染)
    assert "It looks like your message got cut off" in out
    assert "branch (e.g., engine compaction features)" in out
    # 无孤立标点行(流式期间不打印)
    assert "\n:\n" not in out
    assert "\n)\n" not in out


def test_match_commands_prefix_suggestions():
    """补全候选:仅 '/' 开头触发,前缀匹配 name 与 alias。"""
    from codesage.cli.repl import _match_commands

    assert _match_commands("hello") == []
    assert _match_commands("/") == _match_commands("/")  # 全部指令
    names = [c.name for c in _match_commands("/")]
    assert names == [
        "mode", "show-thinking", "compact", "tree", "fork",
        "bookmark", "sessions", "archive", "help", "quit",
    ]
    assert [c.name for c in _match_commands("/co")] == ["compact"]
    assert [c.name for c in _match_commands("/tr")] == ["tree"]
    assert [c.name for c in _match_commands("/h")] == ["help"]  # alias h
    assert [c.name for c in _match_commands("/q")] == ["quit"]  # alias q
    assert _match_commands("/x") == []


def test_graceful_shutdown_exits_via_event_loop():
    """Ctrl+C 在权限询问时:退出必须从事件循环顶层抛出,进程以 130 退出。
    task 内 sys.exit 只会留下 orphan task 异常("Task exception was never
    retrieved"),会话停不下来(回归:权限提示 Ctrl+C)。"""
    import codesage.cli.repl as repl

    class FakeLoop:
        abort = asyncio.Event()

    try:
        with pytest.raises(SystemExit) as exc:
            asyncio.run(graceful_shutdown(FakeLoop(), 130))
        assert exc.value.code == 130
    finally:
        repl._shutdown_started = False  # reset the module guard for other tests


# ---- 13 S2:REPL 空闲自动继续(后台通知到达自动消费)----


class RecordingLLM(MockLLM):
    """记录每次请求的 messages 列表(自动轮注入断言用)。"""

    def __init__(self, script):
        super().__init__(script)
        self.requests = []

    def stream(self, request, model="main"):
        self.requests.append(request.messages)
        return super().stream(request, model)


def test_auto_continue_ready_conditions(monkeypatch):
    """触发条件:唤醒信号 set + 无按键 + 未达 MAX_AUTO_CONTINUE → 自动继续;
    任一不满足 → False(输入优先,达限即停)。"""
    import codesage.cli.repl as repl

    class FakeLoop:
        _notifications_event = asyncio.Event()

    loop = FakeLoop()
    monkeypatch.setattr(repl, "_stdin_pending", lambda: False)
    assert repl._auto_continue_ready(loop, 0) is False  # 信号未置位
    loop._notifications_event.set()
    assert repl._auto_continue_ready(loop, 0) is True
    assert repl._auto_continue_ready(loop, repl.MAX_AUTO_CONTINUE) is False  # 达连续上限
    monkeypatch.setattr(repl, "_stdin_pending", lambda: True)
    assert repl._auto_continue_ready(loop, 0) is False  # 用户按键优先


def test_stdin_pending_posix(monkeypatch):
    """POSIX 分支:select 报告 stdin 可读 → True,否则 False —— 非阻塞,
    绝不调用阻塞的 _read_line。"""
    import sys

    import codesage.cli.repl as repl

    monkeypatch.setattr(repl.sys, "platform", "linux")
    # _stdin_pending 函数体内 import select → 补丁打在真实 select 模块上
    monkeypatch.setattr("select.select", lambda *a: ([sys.stdin], [], []))
    assert repl._stdin_pending() is True
    monkeypatch.setattr("select.select", lambda *a: ([], [], []))
    assert repl._stdin_pending() is False


def test_read_line_posix_skips_prompt_when_drawn(monkeypatch):
    """空闲等待已画过提示符(prompt_drawn)→ POSIX 分支不再画,避免双提示符。"""
    import codesage.cli.repl as repl

    monkeypatch.setattr(repl.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr("builtins.input", lambda prompt: calls.append(prompt) or "hi")
    assert repl._read_line({"prompt_drawn": True}) == "hi"
    assert calls == [""]  # 已画 → 空 prompt
    assert repl._read_line({"prompt_drawn": False}) == "hi"
    assert calls[1] != ""  # 未画 → 正常 prompt


async def test_auto_continue_turn_injects_notification_with_history(tmp_path, monkeypatch):
    """自动继续轮:run(None) 消费后台通知进模型请求;loop.history 刷新为会话
    历史 —— 模型不只见通知 XML(fresh REPL 构造快照为 [] 的实证)。"""
    from codesage.cli.repl import _auto_continue_turn

    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="ok"), StreamEvent(type="done")]],
        monkeypatch=monkeypatch,
    )
    await run_single_turn(loop, "prior question", render=False)  # 会话留下历史
    assert loop.history == []  # 构造快照:历史不会自己长出来
    expected = loop.session.load()
    loop.client = RecordingLLM(
        [lambda i: [StreamEvent(type="text_delta", text="auto"), StreamEvent(type="done")]]
    )
    loop._notifications.put_nowait(
        "<task-notification>\n<status>completed</status>\n<summary>bg done</summary>\n"
        "</task-notification>"
    )
    loop._notifications_event.set()
    await _auto_continue_turn(loop, {"show_thinking": False, "transcript": False}, None)
    assert loop.history == expected  # 刷新为会话线性历史(--continue 同语义)
    assert loop.last_stop_reason == "completed"
    texts = [m.content for m in loop.client.requests[0] if isinstance(m.content, str)]
    assert any("prior question" in t for t in texts)  # 历史在模型请求里
    assert any("bg done" in t for t in texts)  # 通知经 drain 注入也在请求里


@pytest.mark.asyncio
async def test_repl_cross_turn_memory_via_history_reload(tmp_path, monkeypatch):
    """14 修复 REPL 跨轮失忆:用户轮之间重载 loop.history 后,第二轮请求包含
    第一轮对话(此前 loop.history 是构造快照恒为空,第二轮失忆)。"""
    loop = _mock_loop(
        tmp_path,
        [lambda i: [StreamEvent(type="text_delta", text="first"), StreamEvent(type="done")]],
        monkeypatch=monkeypatch,
    )
    loop.client = RecordingLLM(loop.client.script)
    await run_single_turn(loop, "第一次的问题:记住这个代号 ALPHA", render=False)
    assert loop.history == []  # 构造快照:不重载则第二轮失忆(bug 实证)
    # REPL 主循环每轮前重载会话线性历史(镜像 _auto_continue_turn 的刷新)
    loop.history = loop.session.load()
    loop.client.script = [
        lambda i: [StreamEvent(type="text_delta", text="second"), StreamEvent(type="done")]
    ]
    await run_single_turn(loop, "我刚刚的问题是什么?", render=False)
    texts = [
        m.content for m in loop.client.requests[-1] if isinstance(m.content, str)
    ]
    assert any("ALPHA" in t for t in texts)  # 第二轮模型请求带着第一轮上下文
    assert any("第一次的问题" in t for t in texts)
