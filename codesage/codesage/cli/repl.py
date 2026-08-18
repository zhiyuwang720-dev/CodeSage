"""REPL (interactive) and single-shot (non-interactive) modes.

Single-shot mode is how the V1 acceptance test drives the harness: no UI,
ask decisions are denied (safe default).
"""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import sys
import threading
import time
from pathlib import Path

from ..engine import AgentLoop, RunSummary
from ..engine.session import _summarize_run
from ..engine.tokens import estimate_context_tokens
from ..skills.prompt import get_prompt_for_command  # 阶段 14 §6.1:斜杠技能兜底
from .commands import COMMANDS, SlashCommand, find_command
from .render import CYAN, GREY, YELLOW, _c, _glyph, render_message, render_streamed_text_delta
from .statusbar import StatusBar

try:  # 类型标注用(SkillRegistry 仅兜底路径使用,懒解析避免装配环)
    from ..skills import SkillRegistry
except ImportError:  # pragma: no cover — 技能包恒存在,防御性兜底
    SkillRegistry = None  # type: ignore[misc,assignment]

#: 13 S2:连续自动继续轮上限 —— 防后台通知刷屏把 REPL 拖进无限续跑;达限
#: 回等待用户输入,用户下一次输入后计数复位。
MAX_AUTO_CONTINUE = 5


async def run_single_turn(
    loop: AgentLoop,
    user_input: str | None,
    *,
    show_thinking: bool = False,
    transcript: bool = False,  # Ctrl+O expanded mode
    out=None,  # TextIO; default sys.stdout (injectable for tests)
    render: bool = True,  # False for --output-format json (summary only)
    on_after_render: "Callable[[], None] | None" = None,  # status-bar redraw hook
) -> RunSummary:
    """One user input, rendered (also used by acceptance tests).

    user_input=None 为 13 S2 自动继续轮:无用户输入,引擎只消费后台通知等
    异步注入内容(run(None) 语义,REPL 空闲自动继续调用)。

    Returns a RunSummary: is_error mirrors the old bool (exit code 1),
    budget_exceeded/max_turns_exceeded flag the engine's structured stop
    reason (exit code 1). The extraction tail is shared with
    AgentSession.submit (engine/session.py).
    """
    target = out or sys.stdout
    started = time.monotonic()
    has_error = False
    last_text = ""
    llm_calls = 0
    total_tokens = 0

    def _on_stream(ev):
        # Streamed deltas are word-granular (DeepSeek sends "标点+\n" as its
        # own chunk): printing complete lines here emits stray punctuation
        # lines before the body, and the tail buffer render.py expects from
        # the caller doesn't exist here. The finished message is rendered in
        # full by render_message below instead — the thinking fold row stays
        # above the body. (Future UX work may re-add buffered streaming.)
        pass

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
    return _summarize_run(
        loop,
        started=started,
        last_text=last_text,
        has_error=has_error,
        llm_calls=llm_calls,
        total_tokens=total_tokens,
    )


async def repl_loop(
    loop: AgentLoop,
    *,
    cwd: Path,
    show_thinking: bool = False,
    skills: "SkillRegistry | None" = None,  # 阶段 14 §6.1:斜杠技能兜底注册表(None = 禁用)
) -> None:
    _install_signal_handlers(loop)
    # /show-thinking toggles the flag; /mode writes loop.mode via "loop";
    # transcript toggles Ctrl+O expanded rendering for subsequent messages.
    state = {"show_thinking": show_thinking, "loop": loop, "transcript": False}
    if skills is None:
        # 未显式传入(如装配层挂到 loop 上的注册表) → 回退读取
        skills = getattr(loop, "_skills", None)
    state["skills"] = skills  # 14 §6.1:技能兜底查找(内置命令优先)
    state["_bar_redraw"] = None  # 技能 inline 调用时经此重画状态栏(见 _invoke_skill)
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

    # 通知消费(阶段 09 §2.5):状态行走 bar.print_below(滚动区一行)。本函数只被
    # 交互 REPL 调用(cli/__init__.py:201)——无头/单次模式走 single-shot 分支
    # (cli/__init__.py:182-193)根本不进 repl_loop,故 bar 恒已装配;无头模式下
    # 通知仅进 hooks.jsonl + 日志
    loop.on_notification = lambda ntype, message, data: bar.print_below(
        _render_notification(ntype, message)
    )

    def _bar_redraw() -> None:
        # "thinking" reflects the /show-thinking mode switch (transcript
        # expansion), not live model activity — no engine event feeds that
        bar.thinking_on = state["show_thinking"]
        bar.redraw()

    state["_bar_redraw"] = _bar_redraw if bar.enabled else None

    try:
        # 13 S2:连续自动继续轮计数(用户输入后复位)。
        auto_continues = 0
        while True:
            # 13 S2 空闲等待:提示符先画,再轮询「唤醒信号 vs 按键」,谁先到
            # 谁驱动 —— 后台通知到达且 REPL 空闲 → 自动 run(None) 继续;用户
            # 按键 → 调阻塞的 _read_line 读输入。只在 _stdin_pending() 检测
            # 到输入后才调 _read_line(否决的 asyncio.wait 超时取消读线程
            # 方案会留僵尸线程并发抢 stdin,见 handoff team-plan.md)。
            if not state.get("prompt_drawn"):
                print(_prompt_text(state), end="", flush=True)
                state["prompt_drawn"] = True
            while not _stdin_pending():
                if not _auto_continue_ready(loop, auto_continues):
                    await asyncio.sleep(0.05)
                    continue
                auto_continues += 1
                await _auto_continue_turn(
                    loop, state, bar,
                    on_after_render=_bar_redraw if bar.enabled else None,
                )
                # 自动轮输出把提示行滚走了:回到输入行,下一轮重画提示符
                state["prompt_drawn"] = False
                bar.move_to_input()
            try:
                line = await asyncio.to_thread(_read_line, state)
            except (EOFError, KeyboardInterrupt):
                print("\nbye")
                return
            finally:
                state["prompt_drawn"] = False  # 输入已被消费,下一轮重画提示符
            auto_continues = 0  # 用户有输入 → 连续自动轮计数复位
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
        loop.cancel_subagents()  # 13 §6.1 R3:退出统一取消后台子代理


def _render_notification(notification_type: str, message: str) -> str:
    """通知状态行(阶段 09 §2.5):滚动区一行灰字,bar.print_below 的输入。"""
    return _c(f"[{notification_type}] {message}", GREY)


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


# ---- 13 S2:REPL 空闲自动继续(后台通知到达自动消费)----


def _stdin_pending() -> bool:
    """非阻塞检查 stdin 是否有按键(自动继续前哨):Windows 用 msvcrt.kbhit,
    POSIX 用 select。绝不调用阻塞的 _read_line —— 读线程一阻塞,后台通知
    到达也只能等用户下一次输入(否决的取消读线程方案见 repl_loop 注释)。"""
    if sys.platform == "win32":
        import msvcrt

        return msvcrt.kbhit()
    import select

    return bool(select.select([sys.stdin], [], [], 0)[0])


def _auto_continue_ready(loop: AgentLoop, auto_continues: int) -> bool:
    """13 S2:空闲自动继续触发条件 —— 唤醒信号已置位、未达连续自动轮上限,
    且用户没有按键(有输入立即让位,输入优先)。"""
    return (
        loop._notifications_event.is_set()
        and auto_continues < MAX_AUTO_CONTINUE
        and not _stdin_pending()
    )


async def _auto_continue_turn(
    loop: AgentLoop,
    state: dict,
    bar: StatusBar | None,
    *,
    on_after_render: "Callable[[], None] | None" = None,
) -> None:
    """13 S2:一轮自动继续 —— run(None) 消费后台通知,模型无需用户转述即可
    感知后台结果。自动轮上下文:loop.history 是构造快照(fresh REPL 为 []),
    模型只看到通知 XML —— 刷新为会话线性历史(--continue 同语义;刷新一次后
    后续用户轮也带历史,顺带修复 REPL 跨轮失忆)。"""
    loop.abort.clear()
    if bar is not None:
        bar.print_below(_c("[后台任务完成,自动继续…]", GREY))
    if loop.session is not None:
        loop.history = loop.session.load()
    await run_single_turn(
        loop, None,
        show_thinking=state["show_thinking"],
        transcript=state["transcript"],
        on_after_render=on_after_render,
    )


def _match_commands(text: str) -> list[SlashCommand]:
    """Slash-command candidates for *text* (prefix match on name + aliases).

    Only non-empty after the user has typed a leading '/'; used by the
    Windows REPL line editor to drive the tab/arrow completion list.
    """
    if not text.startswith("/"):
        return []
    prefix = text[1:].lower()
    return [
        cmd
        for cmd in COMMANDS
        if cmd.name.startswith(prefix) or any(a.startswith(prefix) for a in cmd.aliases)
    ]


def _candidate_col(prompt: str, buf: list[str]) -> int:
    """Cursor column at the end of the typed input (prompt + buffer)."""
    return len(prompt) + sum(len(c) for c in buf)


def _draw_candidates(
    state: dict, cands: list[SlashCommand], sel: int, prompt: str, buf: list[str]
) -> None:
    """Draw the completion list above the input line; cursor back at the
    input-line end. Absolute rows under the status-bar layout, relative
    movement without one."""
    n = len(cands)
    bar = state.get("sb")
    if bar is not None and bar.enabled:
        top = bar._rows - 1 - n  # input line is rows-1; list sits above it
        for i, cmd in enumerate(cands):
            marker = ">" if i == sel else " "
            print(f"\033[{top + i};1H\033[2K{marker} /{cmd.name}  {cmd.description}", file=sys.stdout)
        col = _candidate_col(prompt, buf)
        print(f"\033[{bar._rows - 1};1H\033[2K{prompt}{''.join(buf)}", end="", file=sys.stdout, flush=True)
    else:
        print(f"\033[{n}A", end="", file=sys.stdout)
        for i, cmd in enumerate(cands):
            marker = ">" if i == sel else " "
            if i > 0:
                print("\r\n", end="", file=sys.stdout)
            print(f"\033[2K{marker} /{cmd.name}  {cmd.description}", end="", file=sys.stdout)
        col = _candidate_col(prompt, buf)
        print(f"\033[1B\033[{col}C", end="", file=sys.stdout, flush=True)


def _clear_candidates(state: dict, n: int, prompt: str, buf: list[str]) -> None:
    """Erase the completion list (n rows above the input line)."""
    if n <= 0:
        return
    bar = state.get("sb")
    if bar is not None and bar.enabled:
        top = bar._rows - 1 - n
        for i in range(n):
            print(f"\033[{top + i};1H\033[2K", end="", file=sys.stdout)
        col = _candidate_col(prompt, buf)
        print(f"\033[{bar._rows - 1};1H\033[2K{prompt}{''.join(buf)}", end="", file=sys.stdout, flush=True)
    else:
        print(f"\033[{n}A", end="", file=sys.stdout)
        for i in range(n):
            if i > 0:
                print("\r\n", end="", file=sys.stdout)
            print("\033[2K", end="", file=sys.stdout)
        col = _candidate_col(prompt, buf)
        print(f"\033[1B\033[{col}C", end="", file=sys.stdout, flush=True)


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
    mid-typing, and a leading '/' opens the command-completion list (tab /
    arrow keys move the selection, Enter sends it, ESC closes it). POSIX:
    plain input() (no completion — use /expand instead).
    """
    if sys.platform != "win32":
        # 13 S2:空闲等待已画过提示符(prompt_drawn)则不再画,避免双提示符
        line = input("" if state.get("prompt_drawn") else _prompt_text(state))
        bar = state.get("sb")
        if bar is not None and bar.enabled:
            bar.after_submit()  # erase the echoed input, re-enter the scroll region
        return line
    import msvcrt

    prompt = _prompt_text(state)
    if not state.get("prompt_drawn"):  # 13 S2:空闲等待已画过则不再画
        print(prompt, end="", flush=True)
    buf: list[str] = []
    candidates: list[SlashCommand] = []
    sel = 0

    def redraw() -> None:
        if candidates:
            _draw_candidates(state, candidates, sel, prompt, buf)

    while True:
        ch = msvcrt.getwch()
        if ch == "\x0f":  # Ctrl+O
            if candidates:
                _clear_candidates(state, len(candidates), prompt, buf)
                candidates.clear()
                sel = 0
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
        if ch == "\t":  # tab: cycle the selection
            if candidates:
                sel = (sel + 1) % len(candidates)
                redraw()
            continue
        if ch == "\x1b":
            c1 = msvcrt.getwch()
            if c1 == "[":
                c2 = msvcrt.getwch()
                if c2 == "A" and candidates:  # up arrow
                    sel = (sel - 1) % len(candidates)
                    redraw()
                elif c2 == "B" and candidates:  # down arrow
                    sel = (sel + 1) % len(candidates)
                    redraw()
                # left/right arrows and other sequences: ignored
            elif c1 != "\x1b":  # bare ESC: close the completion list
                if candidates:
                    _clear_candidates(state, len(candidates), prompt, buf)
                    candidates.clear()
                    sel = 0
            continue
        if ch in ("\r", "\n"):
            if candidates:
                chosen = "/" + candidates[sel].name
                _clear_candidates(state, len(candidates), prompt, buf)
            else:
                chosen = "".join(buf)
            bar = state.get("sb")
            if bar is not None and bar.enabled:
                bar.after_submit()  # erase the echoed input, re-enter the scroll region
            else:
                print(file=sys.stdout)
            return chosen
        if ch == "\x08":  # backspace
            if buf:
                buf.pop()
                print("\b \b", end="", flush=True)
                new = _match_commands("".join(buf))
                if new != candidates:
                    if candidates:
                        _clear_candidates(state, len(candidates), prompt, buf)
                    candidates = new
                    sel = 0
                    redraw()
            continue
        if ch == "\x03":  # Ctrl+C
            raise KeyboardInterrupt
        buf.append(ch)
        print(ch, end="", flush=True)
        new = _match_commands("".join(buf))
        if new != candidates:
            if candidates:
                _clear_candidates(state, len(candidates), prompt, buf)
            candidates = new
            sel = 0
            redraw()


async def _handle_slash_command(loop: AgentLoop, line: str, state: dict) -> bool:
    """Handle one slash command via the CC-09 registry; *state* carries REPL
    flags ({"show_thinking", "loop"}). True = exit the REPL.

    阶段 14 §6.1:find_command 未命中 → 技能兜底查找 —— 内置命令恒优先
    (CC builtInCommandNames 同款),技能 aliases 同样参与;技能不可用户调用
    报错;fork 技能走 §8 隔离子代理;inline 技能解析提示词作为下一轮
    user 消息复用 run_single_turn 既有路径。
    """
    parts = line.strip().split(maxsplit=1)
    cmd = find_command(parts[0])
    if cmd is None:
        return await _invoke_skill_fallback(loop, parts, state)
    args = parts[1].split() if len(parts) > 1 else []
    result = cmd.handler(args, state)
    if inspect.isawaitable(result):  # async handlers (e.g. /compact) awaited here
        result = await result
    return result


async def _invoke_skill_fallback(loop: AgentLoop, parts: list[str], state: dict) -> bool:
    """斜杠命令未命中时尝试技能兜底(14 §6.1);仍未命中 → 打印 unknown。"""
    skills = state.get("skills")
    if skills is None:
        print(f"unknown command: {parts[0]} (try /help)")
        return False
    try:
        skill = skills.get(parts[0].lstrip("/"))  # 技能名 + aliases 参与兜底
    except KeyError:
        print(f"unknown command: {parts[0]} (try /help)")
        return False
    if not skill.user_invocable:
        print(f"skill {skill.name!r} cannot be invoked by the user")
        return False
    args_text = parts[1] if len(parts) > 1 else ""
    if skill.context == "fork":
        # §8 fork 执行:隔离子代理,结果直接回显(14 S6 落地)
        from ..skills.fork import execute_forked_skill  # 阶段 14 S6

        result = await execute_forked_skill(skill, args_text, loop=loop, registry=skills)
        text = result.content if isinstance(result.content, str) else str(result.content)
        print(text)
        return False
    # inline:解析提示词作为下一轮 user 消息,复用 run_single_turn 既有路径
    prompt = await get_prompt_for_command(
        skill,
        args_text,
        session_id=loop.session.session_id if loop.session else "",
        cwd=loop.cwd,
        loop=loop,
    )
    loop.grant_skill_tools(skill.allowed_tools)  # §7.1:技能授权,会话内累积
    await run_single_turn(
        loop,
        prompt,
        show_thinking=state["show_thinking"],
        transcript=state["transcript"],
        on_after_render=state.get("_bar_redraw"),
    )
    return False


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
    # 退出从事件循环顶层抛,而不是在 task 内 sys.exit:task 内抛 SystemExit
    # 会变成无人 retrieve 的 orphan task 异常("Task exception was never
    # retrieved"),进程不会退出,权限询问等场景下会话就停不下来。
    asyncio.get_event_loop().call_soon(sys.exit, code)


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
