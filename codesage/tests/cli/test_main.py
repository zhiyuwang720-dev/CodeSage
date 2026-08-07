"""CLI main() tests: piped stdin, resume, --safe/--allowedTools, exit codes (offline)."""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import pytest

from codesage.cli import main
from codesage.cli import repl as repl_module
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


# ---- 1b. -p/--print + headless auto-detection (C1) ----

def test_stdout_non_tty_prompt_auto_headless(tmp_path, monkeypatch, capsys):
    """stdout not a tty + prompt present → auto single-turn (Kode headlessMode)."""
    _patch_config_dir(monkeypatch, tmp_path)
    loop = FakeLoop()
    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: loop)
    monkeypatch.setattr(sys, "stdin", _Stdin("", is_tty=True))

    assert main(["auto headless"]) == 0
    assert loop.inputs == ["auto headless"]  # single turn ran, stdin untouched


def test_print_reads_piped_stdin(tmp_path, monkeypatch, capsys):
    """-p with no prompt and non-tty stdin → read the pipe."""
    _patch_config_dir(monkeypatch, tmp_path)
    loop = FakeLoop()
    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: loop)
    monkeypatch.setattr(sys, "stdin", _Stdin("from pipe"))

    assert main(["--print"]) == 0
    assert loop.inputs == ["from pipe"]


def test_print_no_input_stdin_tty_exits_1(tmp_path, monkeypatch, capsys):
    """-p with no prompt and tty stdin → error exit 1."""
    _patch_config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "stdin", _Stdin("", is_tty=True))
    called = []
    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: called.append(1))

    assert main(["--print"]) == 1
    assert not called
    err = capsys.readouterr().err
    assert "prompt" in err and "usage" in err


# ---- 1c. --max-budget-usd (C2) ----

def test_max_budget_usd_passthrough(tmp_path, monkeypatch):
    _patch_config_dir(monkeypatch, tmp_path)
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--max-budget-usd", "1.5", "hi"]) == 0
    assert calls[0]["max_budget_usd"] == 1.5


def test_budget_exceeded_exits_1(tmp_path, monkeypatch, capsys):
    """Engine stop message containing 'budget' → stderr note + exit 1."""
    _patch_config_dir(monkeypatch, tmp_path)

    class BudgetLoop(FakeLoop):
        async def run(self, user_input):
            yield assistant_message("Stopped: maximum budget reached.")

    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: BudgetLoop())

    assert main(["--max-budget-usd", "0.01", "hi"]) == 1
    assert "budget" in capsys.readouterr().err.lower()


def test_budget_stop_reason_exits_1_no_sniffing(tmp_path, monkeypatch, capsys):
    """CC-10: structured max_budget stop reason wins over text content."""
    _patch_config_dir(monkeypatch, tmp_path)

    class ReasonLoop(FakeLoop):
        last_stop_reason = "max_budget"

        async def run(self, user_input):
            yield assistant_message("all done")  # no 'budget' in the text

    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: ReasonLoop())

    assert main(["--max-budget-usd", "0.01", "hi"]) == 1
    assert "budget" in capsys.readouterr().err.lower()


def test_max_turns_stop_reason_exits_1(tmp_path, monkeypatch, capsys):
    """CC-10: structured max_turns stop reason → stderr note + exit 1."""
    _patch_config_dir(monkeypatch, tmp_path)

    class TurnsLoop(FakeLoop):
        last_stop_reason = "max_turns"

        async def run(self, user_input):
            yield assistant_message("Stopped: maximum turn count reached.")

    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: TurnsLoop())

    assert main(["--max-turns", "1", "hi"]) == 1
    assert "turns" in capsys.readouterr().err.lower()


# ---- 1d. --output-format json (C3) ----

def test_output_format_json_emits_summary(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    loop = FakeLoop()
    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: loop)

    assert main(["--output-format", "json", "hi"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert set(data) == {
        "session_id",
        "result",
        "num_turns",
        "usage",
        "total_cost_usd",
        "is_error",
        "duration_seconds",
        "max_turns_exceeded",
        "budget_exceeded",
        "permission_denials",
    }
    assert data["result"] == "ok"
    assert data["is_error"] is False
    assert data["num_turns"] >= 1
    assert data["usage"] >= 0
    assert data["budget_exceeded"] is False
    assert data["permission_denials"] == []


# ---- 1e. --debug / --verbose (C4) ----

def test_debug_sets_logging_level(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    _patch_build_loop(monkeypatch, [])
    old = logging.getLogger().level
    try:
        assert main(["--debug", "api", "hi"]) == 0  # filter value accepted
        assert logging.getLogger().level == logging.DEBUG
    finally:
        logging.getLogger().setLevel(old)


def test_verbose_sets_logging_level(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    _patch_build_loop(monkeypatch, [])
    old = logging.getLogger().level
    try:
        assert main(["--verbose", "hi"]) == 0
        assert logging.getLogger().level == logging.INFO
    finally:
        logging.getLogger().setLevel(old)


# ---- 1f. --system-prompt / --system-prompt-file (C5) ----

def test_system_prompt_passthrough(tmp_path, monkeypatch):
    _patch_config_dir(monkeypatch, tmp_path)
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--system-prompt", "be brief", "hi"]) == 0
    assert calls[0]["system_prompt"] == "be brief"


def test_system_prompt_file_reads(tmp_path, monkeypatch):
    _patch_config_dir(monkeypatch, tmp_path)
    f = tmp_path / "prompt.txt"
    f.write_text("from file", encoding="utf-8")
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--system-prompt-file", str(f), "hi"]) == 0
    assert calls[0]["system_prompt"] == "from file"


def test_system_prompt_file_missing_exits_1(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr("codesage.cli.build_loop", lambda **kw: called.append(1))

    assert main(["--system-prompt-file", str(tmp_path / "nope.txt"), "hi"]) == 1
    assert not called
    assert "system-prompt-file" in capsys.readouterr().err


def test_system_prompt_flags_mutually_exclusive_exit_2(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--system-prompt", "x", "--system-prompt-file", "y", "hi"])
    assert exc.value.code == 2  # C6: flag-combo validation failure


# ---- 1g. headless permission semantics documented (C7) ----

def test_docstring_documents_headless_permission_semantics():
    import codesage.cli as cli_module

    assert "ask 决策一律拒绝" in cli_module.__doc__


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
    # the resume banner is printed synchronously; history body rendering is
    # covered in test_render (capsys+asyncio capture timing differs here)
    assert "resuming session-old" in out
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
    # history rendering is covered in test_render; here the resume banner
    # (printed synchronously before asyncio.run) is what capsys reliably sees
    assert "resuming session-x" in capsys.readouterr().out


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


# ---- 5. graceful shutdown (CC-11) ----

class _AbortLoop:
    def __init__(self):
        self.abort = asyncio.Event()


@pytest.mark.asyncio
async def test_graceful_shutdown_aborts_and_exits_with_code(monkeypatch):
    monkeypatch.setattr(repl_module, "_shutdown_started", False)
    exited = []
    monkeypatch.setattr(repl_module.sys, "exit", lambda code: exited.append(code))
    loop = _AbortLoop()

    await repl_module.graceful_shutdown(loop, 130)

    assert loop.abort.is_set()  # running turn/tools get aborted
    assert exited == [130]  # exits with the requested code


@pytest.mark.asyncio
async def test_graceful_shutdown_idempotent(monkeypatch, capsys):
    monkeypatch.setattr(repl_module, "_shutdown_started", False)
    exited = []
    monkeypatch.setattr(repl_module.sys, "exit", lambda code: exited.append(code))
    loop = _AbortLoop()

    await repl_module.graceful_shutdown(loop, 130)
    await repl_module.graceful_shutdown(loop, 130)  # second call: no-op

    assert exited == [130]  # cleanup ran once
    assert capsys.readouterr().out.count("bye") == 1


@pytest.mark.asyncio
async def test_signal_first_aborts_second_force_exits(monkeypatch):
    monkeypatch.setattr(repl_module, "_shutdown_started", False)
    exited = []
    monkeypatch.setattr(repl_module.sys, "exit", lambda code: exited.append(code))
    loop = _AbortLoop()

    handler = repl_module._make_signal_handler(loop)
    handler(None, None)  # first signal: abort the running turn
    assert loop.abort.is_set()
    assert exited == []  # graceful exit scheduled, not forced

    handler(None, None)  # second signal: force exit 130
    assert exited == [130]
    await asyncio.sleep(0)  # drain the scheduled graceful_shutdown task


# ---- 4. --continue (CC-UI: resume history as context, same session file) ----

def test_continue_loads_history_same_session(tmp_path, monkeypatch):
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    session = Session("session-cont", root)
    session.append(user_message("first question"))
    session.append(assistant_message("first answer"))
    os.utime(session.path, (0, 2000))
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--continue", "hi"]) == 0
    assert calls[0]["history"] is not None
    assert len(calls[0]["history"]) == 2
    # --continue keeps the same session file: session_id passed through
    assert calls[0]["session"].path == session.path


def test_continue_no_sessions_exits_1(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    assert main(["--continue", "hi"]) == 1
    assert "no sessions to continue" in capsys.readouterr().err
