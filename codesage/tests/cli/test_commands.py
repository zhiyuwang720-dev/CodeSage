"""CC-09: slash command registry — lookup, aliases, unknown, generated HELP_TEXT."""

import pytest

from codesage.cli.commands import COMMANDS, HELP_TEXT, find_command
from codesage.cli.repl import _handle_slash_command


class _FakeLoop:
    mode = "default"


class _CompactingLoop(_FakeLoop):
    """/compact 分发用假件:记录调用次数,结果可编程。"""

    def __init__(self, result):
        self._result = result
        self.compact_calls = 0

    async def compact_now(self):
        self.compact_calls += 1
        return self._result


def _state(loop=None):
    return {"show_thinking": False, "loop": loop or _FakeLoop()}


def test_find_command_by_name_with_or_without_slash():
    assert find_command("/help").name == "help"
    assert find_command("help") is find_command("/help")
    assert find_command("mode").name == "mode"
    assert find_command("compact").name == "compact"
    assert find_command("quit").name == "quit"
    assert find_command("show-thinking").name == "show-thinking"


def test_find_command_alias():
    assert find_command("h").name == "help"
    assert find_command("q").name == "quit"


def test_find_command_unknown_returns_none():
    assert find_command("bogus") is None
    assert find_command("") is None
    assert find_command("/") is None


def test_help_text_generated_from_registry():
    for cmd in COMMANDS:
        assert f"/{cmd.name}" in HELP_TEXT
        assert cmd.description in HELP_TEXT


@pytest.mark.asyncio
async def test_unknown_command_reported_not_fatal(capsys):
    assert await _handle_slash_command(_FakeLoop(), "/bogus", _state()) is False
    assert "unknown command" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_mode_validates_args_in_handler(capsys):
    loop = _FakeLoop()
    state = _state(loop)
    assert await _handle_slash_command(loop, "/mode yolo", state) is False
    assert loop.mode == "yolo"
    assert await _handle_slash_command(loop, "/mode bogus", state) is False
    assert loop.mode == "yolo"  # unchanged
    assert await _handle_slash_command(loop, "/mode", state) is False
    assert "usage" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_quit_exits_repl(capsys):
    assert await _handle_slash_command(_FakeLoop(), "/quit", _state()) is True
    assert "bye" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_help_command_prints_generated_text(capsys):
    assert await _handle_slash_command(_FakeLoop(), "/help", _state()) is False
    out = capsys.readouterr().out
    assert "/mode" in out and "/compact" in out and "/quit" in out and "/show-thinking" in out


@pytest.mark.asyncio
async def test_compact_command_dispatches_and_reports_success(capsys):
    """§6.3:/compact → await loop.compact_now()(非任务),成功打印一行结果。"""
    loop = _CompactingLoop(result=True)
    assert await _handle_slash_command(loop, "/compact", _state(loop)) is False
    assert loop.compact_calls == 1
    assert "上下文已压缩" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_compact_command_reports_nothing_to_compress(capsys):
    """无可压缩内容 → False,打印「无可压缩内容」。"""
    loop = _CompactingLoop(result=False)
    assert await _handle_slash_command(loop, "/compact", _state(loop)) is False
    assert loop.compact_calls == 1
    assert "无可压缩内容" in capsys.readouterr().out
