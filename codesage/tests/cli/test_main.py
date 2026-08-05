"""CLI main() tests: piped stdin, resume, --safe/--allowedTools, exit codes (offline)."""

import os
import sys
from pathlib import Path

import pytest

from codesage.cli import main
from codesage.cli.repl import _handle_slash_command
from codesage.config import paths
from codesage.core import Session, assistant_message, user_message
from codesage.core.session import find_session, list_sessions, most_recent_session
from codesage.tools import ToolRegistry, get_builtin_tools


class FakeLoop:
    """Minimal loop: records inputs, yields one text answer; tools replaceable."""

    def __init__(self):
        self.tools = ToolRegistry(get_builtin_tools())
        self.mode = "default"
        self.inputs = []

    async def run(self, user_input):
        self.inputs.append(user_input)
        yield assistant_message("ok")


def _patch_build_loop(monkeypatch, calls):
    def fake(**kw):
        calls.append(kw)
        return FakeLoop()

    monkeypatch.setattr("codesage.cli.build_loop", fake)


def _patch_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / ".codesage")


class _Stdin:
    def __init__(self, text, is_tty=False):
        self._text = text
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty

    def read(self):
        return self._text


# ---- 1. non-TTY stdin → single turn ----

def test_stdin_pipe_runs_single_turn(tmp_path, monkeypatch):
    _patch_config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "stdin", _Stdin("hello from pipe"))
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main([]) == 0
    assert calls[0]["request_permission"] is None  # single-shot: no permission UI
    assert calls[0]["project_key"] is None
    # loop ran with the piped content as the prompt
    assert len(calls) == 1


def test_stdin_empty_exits_1(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "stdin", _Stdin("   \n"))
    called = []
    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: called.append(1))

    assert main([]) == 1
    assert not called  # build_loop never reached
    err = capsys.readouterr().err
    assert "usage" in err.lower()


def test_stdin_pipe_error_turn_exits_1(tmp_path, monkeypatch):
    _patch_config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "stdin", _Stdin("hi"))

    class ErrorLoop(FakeLoop):
        async def run(self, user_input):
            yield assistant_message("(provider error: boom)", is_error=True)

    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: ErrorLoop())
    assert main([]) == 1


# ---- 2. sessions: listing, most recent, resume flags ----

def test_list_sessions_sorted_by_mtime(tmp_path):
    root = tmp_path / "sessions"
    top = root / "session-old.jsonl"
    nested = root / "my-proj" / "session-new.jsonl"
    nested.parent.mkdir(parents=True)
    top.write_text("", encoding="utf-8")
    nested.write_text("", encoding="utf-8")
    os.utime(top, (0, 1000))
    os.utime(nested, (0, 2000))

    assert list_sessions(root) == [nested, top]


def test_list_sessions_missing_root(tmp_path):
    assert list_sessions(tmp_path / "nope") == []


def test_most_recent_session(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)
    old, new = root / "s1.jsonl", root / "s2.jsonl"
    old.write_text("", encoding="utf-8")
    new.write_text("", encoding="utf-8")
    os.utime(old, (0, 1000))
    os.utime(new, (0, 2000))
    assert most_recent_session(root) == new
    assert most_recent_session(tmp_path / "nope") is None


def test_find_session_across_project_subdirs(tmp_path):
    root = tmp_path / "sessions"
    nested = root / "proj" / "session-x.jsonl"
    nested.parent.mkdir(parents=True)
    nested.write_text("", encoding="utf-8")
    os.utime(nested, (0, 2000))
    assert find_session(root, "session-x") == nested
    assert find_session(root, "ghost") is None


def test_resume_no_sessions_exits_1(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: called.append(1))

    assert main(["--resume", "hi"]) == 1
    assert not called
    assert "no sessions" in capsys.readouterr().err


def test_session_id_not_found_exits_1(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: called.append(1))

    assert main(["--session-id", "ghost", "hi"]) == 1
    assert not called
    assert "not found" in capsys.readouterr().err


def test_resume_prints_history_and_starts_new(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    session = Session("session-old", root)
    session.append(user_message("previous question"))
    session.append(assistant_message("previous answer"))
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--resume", "hi"]) == 0
    out = capsys.readouterr().out
    assert "previous question" in out
    assert "previous answer" in out
    # new session, not the old file: main omits session_id → build_loop makes a fresh id
    assert "session_id" not in calls[0]
    assert calls[0]["project_key"] is None
    assert calls[0]["request_permission"] is None


def test_resume_project_key_propagated(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    nested = root / "my-proj" / "session-x.jsonl"
    nested.parent.mkdir(parents=True)
    nested.write_text(user_message("old q").to_json() + "\n", encoding="utf-8")
    os.utime(nested, (0, 2000))
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--resume", "hi"]) == 0
    assert calls[0]["project_key"] == "my-proj"
    assert "old q" in capsys.readouterr().out


# ---- 3. --safe / --allowedTools ----

def test_safe_forces_default_mode(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--safe", "--mode", "yolo", "hi"]) == 0
    assert calls[0]["mode"] == "default"
    assert "safe" in capsys.readouterr().err.lower()


def test_safe_root_refused(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)
    called = []
    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: called.append(1))

    assert main(["--safe", "hi"]) == 1
    assert not called
    assert "root" in capsys.readouterr().err


def test_safe_not_root_on_windows(tmp_path, monkeypatch):
    """Windows: admin detection skipped, safe mode runs."""
    _patch_config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--safe", "hi"]) == 0
    assert calls[0]["mode"] == "default"


def test_allowed_tools_filters_registry(tmp_path, monkeypatch):
    _patch_config_dir(monkeypatch, tmp_path)
    loop = FakeLoop()
    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: loop)

    assert main(["--allowedTools", "Bash,Read", "hi"]) == 0
    assert {t.name for t in loop.tools.all()} == {"Bash", "Read"}


def test_disallowed_tools_filters_registry(tmp_path, monkeypatch):
    _patch_config_dir(monkeypatch, tmp_path)
    loop = FakeLoop()
    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: loop)

    assert main(["--disallowedTools", "Bash", "hi"]) == 0
    assert "Bash" not in {t.name for t in loop.tools.all()}
    assert loop.tools.all()  # others survive


def test_allowed_tools_unknown_names_leave_empty(tmp_path, monkeypatch):
    _patch_config_dir(monkeypatch, tmp_path)
    loop = FakeLoop()
    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: loop)

    assert main(["--allowedTools", "Nope", "hi"]) == 0
    assert loop.tools.all() == []


# ---- 4. exit codes / slash commands ----

def test_explicit_prompt_exit_0(tmp_path, monkeypatch):
    _patch_config_dir(monkeypatch, tmp_path)
    _patch_build_loop(monkeypatch, [])
    assert main(["hi"]) == 0


def test_version_exits_0(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "codesage 0.1.0" in capsys.readouterr().out


def test_invalid_mode_exits_2(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "bogus", "hi"])
    assert exc.value.code == 2  # argparse usage error


@pytest.mark.asyncio
async def test_show_thinking_toggle():
    loop = FakeLoop()
    state = {"show_thinking": False}
    assert await _handle_slash_command(loop, "/show-thinking", state) is False
    assert state["show_thinking"] is True
    assert await _handle_slash_command(loop, "/show-thinking", state) is False
    assert state["show_thinking"] is False
