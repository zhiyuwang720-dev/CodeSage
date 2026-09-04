"""三屏流式 TUI(LiveReviewSink)测试。

覆盖: render_frame 纯函数(头部项目/sessionID、流式文本、重试徽标、CJK 宽度、
高度钳制)、sink 状态更新、dispatcher→bridge.on_session_created 的 session_start
接线、cli --live 注入与回落、streaming 开关还原。asyncio_mode=auto。
"""
from __future__ import annotations

import sys
from io import StringIO

from app.services.pr_review.live_sink import LiveReviewSink, display_width, render_frame


def _pane(**kw):
    base = {
        "perspective": "security",
        "status": "waiting",
        "turn_count": 0,
        "findings": 0,
        "session_id": None,
        "retry": None,
        "last_error": None,
        "current_text": "",
        "activity": [],
    }
    base.update(kw)
    return base


def _panes(*items):
    return {p["perspective"]: p for p in items}


def _three_panes(**override):
    panes = _panes(
        _pane(perspective="security"),
        _pane(perspective="architecture"),
        _pane(perspective="quality"),
    )
    panes.update(override)
    return panes


# ── render_frame 纯函数 ──


def test_frame_header_shows_project_and_sessions():
    panes = _three_panes(
        security=_pane(perspective="security", status="thinking", session_id="abc12345-xxxx")
    )
    meta = {"repo": "o/r", "pr_number": 7, "model": "DeepSeek-V4-Flash-0731", "engine": "runtime"}
    frame = render_frame(panes, meta, elapsed=65.0, width=100, height=20)
    header = "\n".join(frame)
    assert "o/r#7" in header
    assert "DeepSeek-V4-Flash-0731" in header
    assert "01:05" in header  # elapsed 65s → mm:ss
    assert "security=abc12345" in header
    assert "security" in frame[3]  # 第一屏标题


def test_frame_streaming_text_grows_in_pane():
    panes = _three_panes(
        security=_pane(perspective="security", status="streaming", current_text="正在审查认证绕过逻辑…")
    )
    frame = render_frame(panes, {}, elapsed=0.0, width=120, height=20)
    assert "正在审查认证绕过逻辑" in "\n".join(frame)


def test_frame_retry_badge_and_done_counts():
    panes = _three_panes(
        security=_pane(perspective="security", status="retrying", retry=(2, 6, "timeout")),
        architecture=_pane(perspective="architecture", status="done", turn_count=6, findings=4),
    )
    joined = "\n".join(render_frame(panes, {}, elapsed=0.0, width=120, height=20))
    assert "重试2/6" in joined
    assert "timeout" in joined
    assert "第6轮" in joined
    assert "4发现" in joined


def test_frame_cjk_truncation_and_width_alignment():
    panes = _three_panes(
        security=_pane(
            perspective="security",
            status="retrying",
            retry=(2, 6, "model_stream_timeout"),
            turn_count=99,
        )
    )
    frame = render_frame(panes, {}, elapsed=0.0, width=100, height=20)
    widths = {display_width(line) for line in frame}
    assert len(widths) == 1  # 所有行等宽, 边框不错位
    assert widths.pop() <= 100
    assert "…" in frame[3]  # 超长标题截断补省略号


def test_frame_height_and_narrow_width_no_crash():
    panes = _three_panes()
    assert render_frame(panes, {}, elapsed=0.0, width=80, height=5)
    assert render_frame(panes, {}, elapsed=0.0, width=10, height=3)


# ── sink 状态更新 ──


def _sink():
    return LiveReviewSink(StringIO(), enabled=True, start_thread=False)


def test_sink_updates_state_from_events():
    sink = _sink()
    sink({"type": "meta", "repo": "o/r", "pr_number": 7, "engine": "runtime", "model": "M"})
    sink({"type": "perspective_start", "perspective": "security"})
    sink({"type": "session_start", "perspective": "security", "session_id": "sess-abc"})
    sink({"type": "assistant_start", "perspective": "security"})
    sink({"type": "token", "perspective": "security", "content": "你好"})
    sink({"type": "token", "perspective": "security", "content": "世界"})
    sink({"type": "tool_call", "perspective": "security", "tool_call": {"name": "Read", "input": {"path": "a.py"}}})
    sink({"type": "llm_retry", "perspective": "security", "attempt": 2, "max_attempts": 6, "error_type": "model_stream_timeout"})
    sink({"type": "perspective_done", "perspective": "security", "turn_count": 4, "findings": 2})

    with sink._lock:
        pane = sink._panes["security"].snapshot()
        meta = dict(sink._meta)
    assert meta["repo"] == "o/r"
    assert pane["session_id"] == "sess-abc"
    assert pane["turn_count"] == 4
    assert pane["findings"] == 2
    assert pane["status"] == "done"
    # token 合并进当前行; 工具/重试/完成事件触发闭合
    assert pane["current_text"] == ""
    assert "你好世界" in pane["activity"]
    assert any("⚙ Read" in line for line in pane["activity"])
    assert any("重试 2/6" in line for line in pane["activity"])
    assert pane["retry"] == (2, 6, "model_stream_timeout")
    assert any("视角完成: 4 轮, 2 发现" in line for line in pane["activity"])


def test_sink_enabled_false_is_noop():
    sink = LiveReviewSink(StringIO(), enabled=False, start_thread=False)
    sink({"type": "assistant_start", "perspective": "security"})
    sink({"type": "token", "perspective": "security", "content": "x"})
    assert sink._panes["security"].turn_count == 0


def test_sink_unknown_perspective_creates_pane():
    sink = _sink()
    sink({"type": "perspective_start", "perspective": "weird"})
    assert "weird" in sink._panes
    assert sink._panes["weird"].status == "thinking"


def test_sink_close_is_idempotent():
    sink = _sink()
    sink.close()
    sink.close()


def test_sink_ignores_unknown_event_types():
    sink = _sink()
    sink({"type": "message", "perspective": "security"})
    sink({"type": "assistant_tombstone", "perspective": "security"})
    assert sink._panes["security"].status == "waiting"


# ── dispatcher → bridge.on_session_created 接线 ──


async def test_dispatcher_emits_session_start(monkeypatch):
    from types import SimpleNamespace

    import app.services.runtime.bridge as bridge_mod
    from app.services.pr_review.runtime_dispatcher import RuntimePerspectiveDispatcher

    class Recorder:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def __call__(self, event: dict) -> None:
            self.events.append(event)

    recorder = Recorder()

    class StubBridge:
        def __init__(self, **kwargs) -> None:
            pass

        async def run(self, **kwargs) -> dict:
            # 模拟 adapter.run 创建 session 后回调
            if kwargs.get("on_session_created") is not None:
                await kwargs["on_session_created"]("sess-abc")
            if kwargs.get("event_sink") is not None:
                await kwargs["event_sink"]({"type": "assistant_start"})
            return {
                "final_payload": {"findings": [], "summary": "s"},
                "session_id": "sess-abc",
                "turn_count": 1,
            }

    monkeypatch.setattr(bridge_mod, "RuntimeBridge", StubBridge)

    dispatcher = RuntimePerspectiveDispatcher(
        llm_service=object(),
        tools={},
        project_id="o/r/1",
        event_sink=recorder,
    )
    ctx = SimpleNamespace(
        pr_key="o/r/1",
        repo="o/r",
        pr_number=1,
        diff_text="diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
        related_files=[],
        git_history=[],
        ci_status=None,
        user_context=None,
    )

    await dispatcher("security", ctx)

    assert [e["type"] for e in recorder.events] == [
        "perspective_start",
        "session_start",
        "assistant_start",
        "perspective_done",
    ]
    assert recorder.events[1] == {
        "type": "session_start",
        "session_id": "sess-abc",
        "perspective": "security",
    }


# ── cli 注入 ──


def _run_cli_main(monkeypatch, tmp_path, *extra_args: str):
    import app.cli as cli
    from app.services.pr_review import command_router

    captured: dict = {}

    async def fake_pipeline(**kwargs):
        captured["options"] = kwargs.get("options") or {}
        captured["event_sink"] = kwargs.get("event_sink")

        class FakeResult:
            review_id = "r"
            status = "completed"
            context_path = "c"
            comments = []

        return FakeResult()

    monkeypatch.setattr(command_router, "run_review_pipeline_async", fake_pipeline)
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(
        "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8"
    )
    rc = cli.main(
        [
            "review",
            "--diff-file",
            str(diff_file),
            "--engine",
            "runtime",
            "--output",
            "json",
            *extra_args,
        ]
    )
    assert rc == 0
    return captured


class _FakeLiveSink:
    """不启线程的 LiveReviewSink 替身, 只验证 cli 接线。"""

    def __init__(self, stream) -> None:
        self.stream = stream

    def __call__(self, event: dict) -> None:
        return None

    def close(self) -> None:
        return None


def test_cli_live_on_tty_injects_live_sink(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.pr_review.live_sink.LiveReviewSink", _FakeLiveSink)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    captured = _run_cli_main(monkeypatch, tmp_path, "--live")
    assert isinstance(captured["event_sink"], _FakeLiveSink)
    assert captured["options"].get("streaming") is True
    # 运行时对象不得进 options(options 会被 build_review_context 持久化)
    assert "event_sink" not in captured["options"]


def test_cli_live_falls_back_to_progress_when_piped(monkeypatch, tmp_path):
    from app.services.pr_review.progress import RuntimeProgressSink

    # 不 patch isatty: pytest 捕获下 stderr 非 tty
    captured = _run_cli_main(monkeypatch, tmp_path, "--live")
    assert isinstance(captured["event_sink"], RuntimeProgressSink)
    assert captured["options"].get("streaming") is True


def test_cli_no_live_keeps_silent(monkeypatch, tmp_path):
    captured = _run_cli_main(monkeypatch, tmp_path, "--no-live")
    assert captured["event_sink"] is None
    assert "streaming" not in captured["options"]
    assert "event_sink" not in captured["options"]


def test_cli_default_piped_silent(monkeypatch, tmp_path):
    captured = _run_cli_main(monkeypatch, tmp_path)
    assert captured["event_sink"] is None
    assert "streaming" not in captured["options"]


# ── command_router: streaming 开关 ──


async def test_streaming_option_toggles_and_restores_setting(monkeypatch):
    import app.services.pr_review.command_router as cr
    from app.core.config import settings

    class FakeDispatcher:
        pass

    captured: dict = {}

    class FakeReview:
        benchmark_comments = []

        def to_result_dict(self) -> dict:
            return {"dummy": True}

    class FakeOrchestrator:
        def __init__(self, dispatcher, **kwargs) -> None:
            self.dispatcher = dispatcher

        async def run(self, ctx):
            captured["during"] = settings.LLM_DISABLE_STREAMING
            return FakeReview()

    monkeypatch.setattr(cr, "ReviewOrchestrator", FakeOrchestrator)
    prev = settings.LLM_DISABLE_STREAMING
    settings.LLM_DISABLE_STREAMING = True
    try:
        result = await cr.run_review_pipeline_async(
            diff_text="diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
            options={
                "engine": "runtime",
                "dispatcher": FakeDispatcher(),
                "streaming": True,
                "repo": "o/r",
                "pr_number": 1,
            },
        )
    finally:
        settings.LLM_DISABLE_STREAMING = prev

    assert captured["during"] is False  # 运行期间已临时关闭
    assert settings.LLM_DISABLE_STREAMING is prev  # 结束后还原
    assert result.status == "completed"
