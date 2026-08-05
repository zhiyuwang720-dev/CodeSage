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
