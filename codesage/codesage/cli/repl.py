"""REPL (interactive) and single-shot (non-interactive) modes.

Single-shot mode is how the V1 acceptance test drives the harness: no UI,
ask decisions are denied (safe default).

交互 REPL 由 OpenCode 风格全屏应用(cli/app.py,prompt_toolkit Application)
驱动:可滚动历史区、边框输入框(占位符 + 轮换灰字建议)、``/`` 自动补全弹窗、
框下 ``Mode · Model`` 信息行、底部状态栏、turn 进行中计时行。权限询问为
应用内 y/n/r 按键。本模块保留单轮渲染(run_single_turn)、斜杠命令分发
(_handle_slash_command/技能兜底)与补全数据源(_SlashCompleter)。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import sys
import time
from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from ..engine import AgentLoop, RunSummary
from ..engine.session import _summarize_run
from ..skills.prompt import get_prompt_for_command  # 阶段 14 §6.1:斜杠技能兜底
from ..skills.state import add_invoked_skill  # 14 §10.1:inline 执行前记录
from .commands import COMMANDS, SlashCommand, find_command
from .render import CYAN, GREY, _c, _glyph, render_message

try:  # 类型标注用(SkillRegistry 仅兜底路径使用,懒解析避免装配环)
    from ..skills import SkillRegistry
except ImportError:  # pragma: no cover — 技能包恒存在,防御性兜底
    SkillRegistry = None  # type: ignore[misc,assignment]


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
    """交互 REPL:委托 OpenCode 风格全屏应用(cli/app.py)。

    全屏布局(历史区/边框输入框/信息行/状态栏)与键位见 app.py;本模块保留
    单轮渲染 run_single_turn、斜杠命令分发与补全数据源。
    """
    from .app import CodeSageApp  # 函数级 import:app ← repl 循环引用

    _install_signal_handlers(loop)
    loop.steer_queue = asyncio.Queue()
    app = CodeSageApp(loop, cwd=cwd, show_thinking=show_thinking, skills=skills)
    await app.run()


def _render_notification(notification_type: str, message: str) -> str:
    """通知状态行(阶段 09 §2.5):灰字一行,交互 REPL 直接打印进输出区。"""
    return _c(f"[{notification_type}] {message}", GREY)


def _drain_steer_queue(loop: AgentLoop) -> str | None:
    """PI-06 followUp: re-run what the engine did NOT consume of the steer queue.

    The steer queue is the single source of truth — inputs the engine already
    injected mid-run are gone; leftovers become the next prompt exactly once.
    (全屏应用路径:turn 进行中输入经 accept_handler 直接入队,见 cli/app.py。)
    """
    if loop.steer_queue is None:
        return None
    try:
        return loop.steer_queue.get_nowait()
    except asyncio.QueueEmpty:
        return None


def _match_commands(text: str) -> list[SlashCommand]:
    """Slash-command candidates for *text* (prefix match on name + aliases).

    Only non-empty after the user has typed a leading '/'; consumed by the
    prompt_toolkit completer (_SlashCompleter) to drive the popup list.
    """
    if not text.startswith("/"):
        return []
    prefix = text[1:].lower()
    return [
        cmd
        for cmd in COMMANDS
        if cmd.name.startswith(prefix) or any(a.startswith(prefix) for a in cmd.aliases)
    ]


class _SlashCompleter(Completer):
    """prompt_toolkit 斜杠补全(OpenCode 风格):``/`` 开头时对内置命令
    (name + aliases,复用 _match_commands)与可用技能(name + aliases)做前缀
    补全;其它输入无补全。display_meta 提供描述列,选中高亮由样式承担。"""

    def __init__(self, commands: list[SlashCommand], skills: "SkillRegistry | None" = None):
        self.commands = commands
        self.skills = skills

    def get_completions(self, document: Document, complete_event) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        prefix = text[1:]
        for cmd in _match_commands(text):
            yield Completion(
                text=cmd.name,
                start_position=-len(prefix),
                display=f"/{cmd.name}",
                display_meta=cmd.description,
            )
        if self.skills is not None:
            for skill in self.skills.all():
                if skill.name.startswith(prefix) or any(a.startswith(prefix) for a in skill.aliases):
                    yield Completion(
                        text=skill.name,
                        start_position=-len(prefix),
                        display=f"/{skill.name}",
                        display_meta=skill.description,
                    )


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
    # §10.1:斜杠 inline 执行前记录(压缩后恢复用;主会话 agent_id=None)
    add_invoked_skill(skill.name, prompt, agent_id=getattr(loop, "_agent_name", None) or None)
    await run_single_turn(
        loop,
        prompt,
        show_thinking=state["show_thinking"],
        transcript=state["transcript"],
        on_after_render=state.get("_bar_redraw"),
    )
    return False


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
