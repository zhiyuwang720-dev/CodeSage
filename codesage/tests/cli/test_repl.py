"""Single-shot mode tests (mock LLM, offline)."""

import io

import pytest

from codesage.ai import StreamEvent
from codesage.cli.assemble import build_loop
from codesage.cli.repl import run_single_turn
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
