"""spec §6 进度输出: RuntimeProgressSink 事件 → stderr 进度行。

事件契约对齐 app/services/runtime/query_loop._emit_event:
event_sink = Callable[[dict], Any], 同步或异步均可。
"""
from __future__ import annotations

from io import StringIO

from app.services.pr_review.progress import RuntimeProgressSink
from app.services.pr_review.runtime_dispatcher import tag_event_sink


def test_sink_prints_turn_and_retry_lines():
    stream = StringIO()
    sink = RuntimeProgressSink(stream)
    sink({"type": "perspective_start", "perspective": "security"})
    sink({"type": "assistant_start", "perspective": "security"})
    sink({"type": "token", "content": "不要打印正文", "perspective": "security"})
    sink({"type": "assistant_start", "perspective": "security"})
    sink({"type": "done", "perspective": "security"})
    sink({"type": "llm_retry", "perspective": "security", "attempt": 2, "max_attempts": 5, "error_type": "network_error"})
    sink({"type": "perspective_done", "perspective": "security", "turn_count": 6, "findings": 4})
    out = stream.getvalue()
    lines = out.splitlines()
    # 视角开始/完成 + 回合计数
    assert any("security: 视角开始" in line for line in lines)
    assert any("第 1 轮 LLM 调用…" in line for line in lines)
    assert any("第 2 轮 LLM 调用…" in line for line in lines)
    assert any("本轮模型响应完成" in line for line in lines)
    assert any("视角完成: 6 轮, 4 条发现" in line for line in lines)
    # 重试含 attempt/max 与错误类型
    assert any("LLM 重试 2/5: network_error" in line for line in lines)
    # 每行带 [runtime ..s] 前缀
    assert all("[runtime " in line for line in lines)
    # token 事件不产生进度行
    assert not any("正文" in line for line in lines)


def test_sink_prints_error_line():
    stream = StringIO()
    sink = RuntimeProgressSink(stream)
    sink({"type": "error", "perspective": "quality", "message_text": "网关超时"})
    assert "quality: 错误: 网关超时" in stream.getvalue()


def test_sink_enabled_false_is_noop():
    stream = StringIO()
    sink = RuntimeProgressSink(stream, enabled=False)
    sink({"type": "assistant_start", "perspective": "security"})
    sink({"type": "perspective_done", "perspective": "security"})
    assert stream.getvalue() == ""


def test_sink_ignores_unknown_event_types():
    stream = StringIO()
    sink = RuntimeProgressSink(stream)
    sink({"type": "reasoning_delta", "perspective": "security"})
    sink({"type": "message", "perspective": "security"})
    sink({"type": "assistant_tombstone", "perspective": "security"})
    assert stream.getvalue() == ""


async def test_tag_event_sink_stamps_perspective_sync_inner():
    seen = []
    sink = tag_event_sink(seen.append, "security")
    assert sink is not None
    await sink({"type": "assistant_start"})
    await sink({"type": "done", "turn_count": 3})
    assert seen == [
        {"type": "assistant_start", "perspective": "security"},
        {"type": "done", "turn_count": 3, "perspective": "security"},
    ]
    # 原始事件 dict 不被就地改写
    assert "perspective" not in {"type": "done", "turn_count": 3}


async def test_tag_event_sink_awaits_async_inner():
    seen = []

    async def inner(event: dict):
        seen.append(event)

    sink = tag_event_sink(inner, "architecture")
    await sink({"type": "perspective_start"})
    assert seen == [{"type": "perspective_start", "perspective": "architecture"}]


def test_tag_event_sink_none_passthrough():
    assert tag_event_sink(None, "security") is None


async def test_dispatcher_forwards_tagged_events_to_sink(monkeypatch):
    """集成: RuntimePerspectiveDispatcher.__call__ 把视角名打进事件并透传给 event_sink。

    用 stub bridge 替代真实 LLM 调用(不触网), 验证 perspective_start / 回合事件 /
    perspective_done(含 turn_count 与 findings)都到达 sink。
    """
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
            # 模拟 query_loop 把一次 LLM 回合事件打到 bridge 收到的 event_sink 上
            if kwargs.get("event_sink") is not None:
                await kwargs["event_sink"]({"type": "assistant_start"})
            return {
                "final_payload": {
                    "findings": [
                        {"file_path": "a.py", "confidence": 0.9},
                        {"file_path": "a.py", "confidence": 0.7},
                    ],
                    "summary": "s",
                },
                "session_id": "sess-1",
                "turn_count": 2,
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

    result = await dispatcher("security", ctx)

    assert [e["type"] for e in recorder.events] == [
        "perspective_start",
        "assistant_start",
        "perspective_done",
    ]
    assert all(e.get("perspective") == "security" for e in recorder.events)
    done = recorder.events[-1]
    assert done["turn_count"] == 2
    assert done["findings"] == 2
    assert result["from_agent"] == "security"
    assert result["context_data"]["turn_count"] == 2


def _run_cli_main(monkeypatch, tmp_path, *extra_args: str):
    """用 fake 异步 pipeline 跑 app.cli.main, 返回 fake 收到的 options。"""
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
    diff_file.write_text("diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")
    rc = cli.main(["review", "--diff-file", str(diff_file), "--engine", "runtime",
                   "--output", "json", *extra_args])
    assert rc == 0
    return captured


def test_cli_progress_flag_injects_sink(monkeypatch, tmp_path):
    from app.services.pr_review.progress import RuntimeProgressSink

    captured = _run_cli_main(monkeypatch, tmp_path, "--progress")
    assert isinstance(captured["event_sink"], RuntimeProgressSink)
    # sink 不得进 options(options 会被 build_review_context 持久化, 非 JSON 类型会崩)
    assert "event_sink" not in captured["options"]


def test_cli_no_progress_flag_keeps_silent(monkeypatch, tmp_path):
    captured = _run_cli_main(monkeypatch, tmp_path, "--no-progress")
    assert captured["event_sink"] is None
    assert "event_sink" not in captured["options"]
