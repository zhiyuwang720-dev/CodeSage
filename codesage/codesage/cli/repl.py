"""REPL (interactive) and single-shot (non-interactive) modes.

Single-shot mode is how the V1 acceptance test drives the harness: no UI,
ask decisions are denied (safe default).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..engine import AgentLoop
from ..engine.tokens import estimate_context_tokens
from .commands import find_command
from .render import CYAN, GREY, YELLOW, _c, _glyph, render_message, render_streamed_text_delta
from .statusbar import StatusBar


@dataclass
class RunSummary:
    """Machine-readable outcome of one single-turn run (--output-format json)."""

    session_id: str
    result: str
    num_turns: int
    usage: int
    total_cost_usd: float
    is_error: bool
    duration_seconds: float
    budget_exceeded: bool = False
    max_turns_exceeded: bool = False

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "result": self.result,
            "num_turns": self.num_turns,
            "usage": self.usage,
            "total_cost_usd": self.total_cost_usd,
            "is_error": self.is_error,
            "duration_seconds": self.duration_seconds,
            "max_turns_exceeded": self.max_turns_exceeded,
        }


async def run_single_turn(
    loop: AgentLoop,
    user_input: str,
    *,
    show_thinking: bool = False,
    transcript: bool = False,  # Ctrl+O expanded mode
    out=None,  # TextIO; default sys.stdout (injectable for tests)
    render: bool = True,  # False for --output-format json (summary only)
    on_after_render: "Callable[[], None] | None" = None,  # status-bar redraw hook
) -> RunSummary:
    """One user input, rendered (also used by acceptance tests).

    Returns a RunSummary: is_error mirrors the old bool (exit code 1),
    budget_exceeded/max_turns_exceeded flag the engine's structured stop
    reason (exit code 1).
    """
    target = out or sys.stdout
    started = time.monotonic()
    has_error = False
    last_text = ""
    llm_calls = 0
    total_tokens = 0

    def _on_stream(ev):
        # live text_deltas print complete lines as they arrive (CC behavior)
        if render and ev.type == "text_delta" and ev.text:
            render_streamed_text_delta(ev.text, target)
            if on_after_render is not None:
                on_after_render()

    def _on_tool_event(event, name, payload):
        # PI-01 wiring: a lightweight status line when a tool starts running.
        # The end state is rendered by the tool_result message (✓/✗) instead.
        if render and event == "start":
            print(_c(f"  {_glyph('●', target)} {name} running…", GREY), file=target, flush=True)
            if on_after_render is not None:
                on_after_render()

    prev_on_stream = getattr(loop, "on_stream", None)
    prev_on_tool_event = getattr(loop, "on_tool_event", None)
    if render:
        loop.on_stream = _on_stream  # type: ignore[attr-defined]
        loop.on_tool_event = _on_tool_event  # type: ignore[attr-defined]
    try:
        async for message in loop.run(user_input):
            has_error = has_error or (message.role == "assistant" and message.is_error)
            if render:
                render_message(message, out=target, transcript=transcript)
                if on_after_render is not None:
                    on_after_render()
            if message.role != "assistant":
                continue
            if message.usage is not None:
                llm_calls += 1
                total_tokens += message.usage.total_tokens
            if isinstance(message.content, str):
                last_text = message.content
            else:
                text = "\n".join(b.text or "" for b in message.content if b.type == "text")
                if text:
                    last_text = text
    finally:
        loop.on_stream = prev_on_stream  # type: ignore[attr-defined]
        loop.on_tool_event = prev_on_tool_event  # type: ignore[attr-defined]
    client = getattr(loop, "client", None)
    # CC-10: prefer the engine's structured stop reason over text sniffing.
    stop_reason = getattr(loop, "last_stop_reason", None)
    budget_exceeded = stop_reason == "max_budget"
    max_turns_exceeded = stop_reason == "max_turns"
    if stop_reason is None:
        # AgentLoop.last_stop_reason not landed yet — legacy fallback: text
        # sniff + cost check (remove once the engine sets the reason).
        budget_exceeded = "budget" in last_text.lower() or (
            getattr(loop, "max_budget_usd", None) is not None
            and getattr(client, "total_cost", None) is not None
            and client.total_cost[0] >= loop.max_budget_usd
        )
    return RunSummary(
        session_id=loop.session.path.stem if getattr(loop, "session", None) is not None else "",
        result=last_text,
        num_turns=max(llm_calls, 1),
        usage=total_tokens,
        total_cost_usd=float(getattr(client, "total_cost", [0.0])[0]),
        is_error=has_error,
        duration_seconds=time.monotonic() - started,
        budget_exceeded=budget_exceeded,
        max_turns_exceeded=max_turns_exceeded,
    )


async def repl_loop(
    loop: AgentLoop,
    *,
    cwd: Path,
    show_thinking: bool = False,
) -> None:
    _install_signal_handlers(loop)
    # /show-thinking toggles the flag; /mode writes loop.mode via "loop";
    # transcript toggles Ctrl+O expanded rendering for subsequent messages.
    state = {"show_thinking": show_thinking, "loop": loop, "transcript": False}
    # steer queue is the single source of truth for mid-run inputs (PI-06):
    # the capture thread appends to captured_lines (GIL-atomic); the main
    # thread transfers them into the queue before/after each turn, and the
    # followUp loop only re-runs what the engine did NOT consume.
    loop.steer_queue = asyncio.Queue()
    state["captured_lines"] = []
    capture_gate = threading.Event()
    _install_mid_run_input_capture(loop, state, capture_gate)

    # Bottom status bar (branch | model | thinking | session | ctx meter) —
    # pinned below the input line via an ANSI scroll region; inert off-tty.
    # The ctx meter reads the loop's active message list, so a compaction
    # visibly drops the bar (review R1: the meter must be wired).
    bar = StatusBar(model_name=_model_display_name(loop), cwd=str(cwd))
    bar._meter = lambda: estimate_context_tokens(loop._active_messages or []).tokens
    state["sb"] = bar
    bar.enable()  # clears the screen — the banner must print AFTER enable
    print(_c("CodeSage — V1 (type /help for commands, Ctrl+C to interrupt, Ctrl+O to expand)", CYAN))
    bar.move_to_input()

    def _bar_redraw() -> None:
        # "thinking" reflects the /show-thinking mode switch (transcript
        # expansion), not live model activity — no engine event feeds that
        bar.thinking_on = state["show_thinking"]
        bar.redraw()

    try:
        while True:
            try:
                line = await asyncio.to_thread(_read_line, state)
            except (EOFError, KeyboardInterrupt):
                print("\nbye")
                return
            if line is None:
                continue  # a hotkey was handled (e.g. Ctrl+O toggled transcript)
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if await _handle_slash_command(loop, line, state):
                    return
                continue
            pending = line
            while pending:
                loop.abort.clear()
                capture_gate.set()  # capture keystrokes only while a turn runs
                try:
                    summary = await run_single_turn(
                        loop, pending,
                        show_thinking=state["show_thinking"],
                        transcript=state["transcript"],
                        on_after_render=_bar_redraw if bar.enabled else None,
                    )
                finally:
                    capture_gate.clear()
                # followUp: transfer captured lines into the queue, then re-run
                # whatever the engine did not consume (single source of truth)
                _transfer_captured(loop, state)
                pending = _drain_steer_queue(loop)
            bar.move_to_input()  # back to the fixed prompt line
    finally:
        bar.disable()


def _model_display_name(loop: AgentLoop) -> str:
    """Resolve the loop's model pointer to the concrete model name."""
    try:
        client = getattr(loop, "client", None)
        if client is not None:
            return client.resolve_profile(getattr(loop, "model", "main")).model
    except Exception:
        pass
    return getattr(loop, "model", "main")


def _install_mid_run_input_capture(loop: AgentLoop, state: dict, gate: "threading.Event") -> None:
    """PI-06: while a turn runs, keystrokes accumulate instead of being lost.

    The capture thread only reads stdin while *gate* is set (i.e. inside
    run_single_turn) — never during _read_line, so it cannot race the main
    prompt. Complete lines append to state["captured_lines"] (GIL-atomic);
    the main thread transfers them into the steer queue. On POSIX this is
    best-effort (select); on Windows msvcrt.
    """
    def _capture():
        if sys.platform == "win32":
            import msvcrt

            buf: list[str] = []
            while True:
                if not gate.is_set():
                    import time

                    time.sleep(0.05)
                    continue
                if not msvcrt.kbhit():
                    import time

                    time.sleep(0.05)
                    continue
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    if buf:
                        state.setdefault("captured_lines", []).append("".join(buf).strip())
                        buf = []
                elif ch == "\x08":
                    if buf:
                        buf.pop()
                elif ch == "\x03":
                    buf = []  # interrupt: don't carry leftovers into the next line
                elif ch not in ("\x1b", "\x0f"):
                    buf.append(ch)
        else:
            # POSIX best-effort: select on stdin, read one line at a time
            import select

            while True:
                import time

                if not gate.is_set():
                    time.sleep(0.05)
                    continue
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    text = line.strip()
                    if text:
                        state.setdefault("captured_lines", []).append(text)

    thread = threading.Thread(target=_capture, daemon=True)
    thread.start()


def _transfer_captured(loop: AgentLoop, state: dict) -> None:
    """Move captured keystrokes into the steer queue (main thread, asyncio-safe)."""
    captured = state.pop("captured_lines", [])
    for text in captured:
        if text and loop.steer_queue is not None:
            loop.steer_queue.put_nowait(text)


def _drain_steer_queue(loop: AgentLoop) -> str | None:
    """PI-06 followUp: re-run what the engine did NOT consume of the steer queue.

    The steer queue is the single source of truth — inputs the engine already
    injected mid-run are gone; leftovers become the next prompt exactly once.
    """
    if loop.steer_queue is None:
        return None
    try:
        return loop.steer_queue.get_nowait()
    except asyncio.QueueEmpty:
        return None


def _prompt_text(state: dict) -> str:
    """The prompt string; no leading newline under the status-bar layout —
    the LF would push the cursor from the input line onto the bar row
    (review R2: the bar layout owns its own line separation)."""
    bar = state.get("sb")
    lead = "" if bar is not None and bar.enabled else "\n"
    return _c(f"{lead}{_prompt_mark()} ", CYAN)


def _read_line(state: dict) -> str | None:
    """Read one input line; None when a hotkey was consumed.

    Windows: msvcrt raw input so Ctrl+O (0x0F) toggles the transcript mode
    mid-typing. POSIX: plain input() (no Ctrl+O — use /expand instead).
    """
    if sys.platform != "win32":
        line = input(_prompt_text(state))
        bar = state.get("sb")
        if bar is not None and bar.enabled:
            bar.after_submit()  # erase the echoed input, re-enter the scroll region
        return line
    import msvcrt

    prompt = _prompt_text(state)
    print(prompt, end="", flush=True)
    buf: list[str] = []
    while True:
        ch = msvcrt.getwch()
        if ch == "\x0f":  # Ctrl+O
            state["transcript"] = not state["transcript"]
            print("\r" + " " * (len(prompt) + sum(len(c) for c in buf)) + "\r", end="", flush=True)
            bar = state.get("sb")
            if bar is not None and bar.enabled:
                # status-bar layout: the note goes into the scroll region,
                # then the prompt line is re-drawn in place
                bar.print_below(_c(f"[transcript {'on' if state['transcript'] else 'off'}]", YELLOW))
                bar.move_to_input()
            else:
                print(_c(f"\n[transcript {'on' if state['transcript'] else 'off'}]", YELLOW), file=sys.stdout)
            print(prompt + "".join(buf), end="", flush=True)
            continue
        if ch in ("\r", "\n"):
            bar = state.get("sb")
            if bar is not None and bar.enabled:
                bar.after_submit()  # erase the echoed input, re-enter the scroll region
            else:
                print(file=sys.stdout)
            return "".join(buf)
        if ch == "\x08":  # backspace
            if buf:
                buf.pop()
                print("\b \b", end="", flush=True)
            continue
        if ch == "\x03":  # Ctrl+C
            raise KeyboardInterrupt
        if ch == "\x1b":  # ESC: ignore sequences for now
            continue
        buf.append(ch)
        print(ch, end="", flush=True)


async def _handle_slash_command(loop: AgentLoop, line: str, state: dict) -> bool:
    """Handle one slash command via the CC-09 registry; *state* carries REPL
    flags ({"show_thinking", "loop"}). True = exit the REPL."""
    parts = line.strip().split(maxsplit=1)
    cmd = find_command(parts[0])
    if cmd is None:
        print(f"unknown command: {parts[0]} (try /help)")
        return False
    args = parts[1].split() if len(parts) > 1 else []
    return cmd.handler(args, state)


def _prompt_mark() -> str:
    """REPL prompt glyph, ASCII '>' when the output encoding can't render it."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "❯".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return ">"
    return "❯"


#: Idempotency guard for graceful_shutdown — one cleanup per process.
_shutdown_started = False


async def graceful_shutdown(loop: AgentLoop, code: int = 130) -> None:
    """CC-11: abort the running turn and exit *code*. Idempotent; cleanup has
    a 5s budget — on timeout the process is force-exited (failsafe)."""
    global _shutdown_started
    if _shutdown_started:
        return
    _shutdown_started = True
    loop.abort.set()  # running tools observe this at the next checkpoint

    async def _cleanup() -> None:
        # Session appends are fsync'd per message — nothing left to flush;
        # aborting in-flight work is done by loop.abort above.
        print("\nbye")

    try:
        await asyncio.wait_for(_cleanup(), timeout=5)
    except asyncio.TimeoutError:
        os._exit(code)  # failsafe: cleanup budget exhausted — force exit
    sys.exit(code)


def _make_signal_handler(loop: AgentLoop):
    """First signal: abort the running turn + graceful exit; second: force exit 130."""

    def on_signal(signum, frame):
        if loop.abort.is_set() or _shutdown_started:
            sys.exit(130)  # second signal (or already shutting down): force exit
        loop.abort.set()  # first: abort the running turn
        try:
            asyncio.get_event_loop().create_task(graceful_shutdown(loop, 130))
        except RuntimeError:
            sys.exit(130)  # no running event loop — nothing left to clean

    return on_signal


def _install_signal_handlers(loop: AgentLoop) -> None:
    """SIGINT/SIGTERM/SIGBREAK(win32): first aborts + graceful exit, second exits 130."""
    on_signal = _make_signal_handler(loop)
    sigs = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        sigs.append(signal.SIGBREAK)
    for sig in sigs:
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass  # not in main thread / unsupported platform — best effort


def _install_single_shot_sigint(loop: AgentLoop) -> None:
    """Single-shot mode: SIGINT aborts the turn and exits 130 directly."""

    def on_sigint(signum, frame):
        loop.abort.set()
        sys.exit(130)

    try:
        signal.signal(signal.SIGINT, on_sigint)
    except (ValueError, OSError):
        pass  # not in main thread / unsupported platform — best effort
