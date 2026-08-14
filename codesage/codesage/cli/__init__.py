"""CLI (phase 07): interactive REPL + single-shot mode. V1 acceptance entry.

Exit codes: 0 success; 1 LLM error turn / USD budget exceeded / max turns
exceeded / empty piped stdin / --print with no input / resume target
missing / safe-mode root refusal / --system-prompt-file unreadable; 2
argparse usage errors.

单轮/headless 模式(--print/--headless,或 stdout 非 tty 且输入存在)无权限
UI:ask 决策一律拒绝并回传模型(可用 --mode yolo + --allowedTools 精确放行)。

--max-budget-usd 触限时打印 "Error: Exceeded USD budget" 到 stderr 并 exit 1
(比 Kode print 模式的 exit 0 更符合脚本语义:非零表示预算未完成)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .. import __version__
from ..config import paths
from ..core import Session, SessionMessage, find_session, most_recent_session
from ..core.session import (
    find_open_operations,
    lane_names,
    linear_messages,
    numbered_entries,
)
from ..engine import AgentSession
from ..engine.compaction import summary_message
from .assemble import apply_tool_filter, build_loop, session_root
from .permission_prompt import request_permission
from .render import CYAN, DIM, _c, render_message
from .repl import _install_single_shot_sigint, repl_loop, run_single_turn


def _configure_logging(verbose: bool, debug: bool) -> None:
    """--debug → DEBUG, --verbose → INFO, both on stderr. The --debug filter
    value is accepted but not used (ponytail: root-level DEBUG, per-module
    filters when a category filter ever needs to exist)."""
    level = logging.DEBUG if debug else (logging.INFO if verbose else None)
    if level is None:
        return
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codesage", description="CodeSage harness (V1)")
    parser.add_argument("prompt", nargs="?", help="single-shot prompt (omit for REPL)")
    parser.add_argument("--cwd", type=Path, default=paths.cwd(), help="working directory")
    parser.add_argument("--mode", default="default", choices=["plan", "default", "yolo"])
    parser.add_argument("--model", default="main")
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--max-budget-usd", type=float, default=None,
                        help="stop after this USD spend (single-turn; exceeded → exit 1)")
    parser.add_argument("--show-thinking", action="store_true")
    parser.add_argument("--safe", action="store_true", help="lock permission mode to default (never yolo)")
    parser.add_argument("-p", "--print", action="store_true",
                        help="single-turn non-interactive mode (no permission UI; ask decisions are denied)")
    parser.add_argument("--headless", action="store_true", help="synonym for --print")
    parser.add_argument("--output-format", choices=["text", "json"], default="text",
                        help="json: emit one result object to stdout after the turn (forces single-turn)")
    parser.add_argument("--debug", nargs="?", const="", default=None, metavar="FILTER",
                        help="debug logging to stderr (optional category filter)")
    parser.add_argument("--verbose", action="store_true", help="INFO logging to stderr")
    prompt_group = parser.add_mutually_exclusive_group()
    prompt_group.add_argument("--system-prompt", metavar="TEXT", help="override the system prompt")
    prompt_group.add_argument("--system-prompt-file", metavar="PATH", help="read the system prompt from a file")
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument("-c", "--continue", dest="continue_flag", action="store_true",
                              help="continue the most recent conversation (history as context, same session file)")
    resume_group.add_argument("--resume", action="store_true", help="resume the most recent session (summary + new session)")
    resume_group.add_argument("--session-id", metavar="ID", help="resume a specific session by id")
    parser.add_argument("--lane", metavar="NAME", help="continue/resume along a named branch (spec §4.4/§5; unknown lane → exit 1)")
    parser.add_argument("--allowedTools", metavar="N1,N2", help="comma-separated tool allowlist")
    parser.add_argument("--disallowedTools", metavar="N1,N2", help="comma-separated tool denylist")
    parser.add_argument("--version", action="version", version=f"codesage {__version__}")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose, args.debug is not None)

    # single-turn detection (Kode headlessMode semantics): explicit
    # --print/--headless, or stdout not a tty with a prompt or piped stdin.
    print_mode = args.print or args.headless or (args.output_format == "json")
    prompt = args.prompt
    stdin_read = False
    if print_mode or not sys.stdout.isatty() or not sys.stdin.isatty():
        if prompt is None and not sys.stdin.isatty():
            prompt = sys.stdin.read().strip() or None
            stdin_read = True
        if prompt is None and (print_mode or stdin_read):
            why = (
                "--print: no prompt given — pass a prompt argument or pipe stdin"
                if print_mode
                else "no prompt given — stdin is empty"
            )
            print(f"{why} (usage below)", file=sys.stderr)
            print(parser.format_usage(), file=sys.stderr)
            return 1
        if prompt is not None and (print_mode or not sys.stdout.isatty()):
            print_mode = True

    system_prompt = args.system_prompt
    if args.system_prompt_file:
        try:
            system_prompt = Path(args.system_prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"cannot read --system-prompt-file: {exc}", file=sys.stderr)
            return 1

    if args.safe:
        if args.mode != "default":
            print(f"--safe: forcing --mode {args.mode} -> default", file=sys.stderr)
        print("safe mode: permission mode locked to default", file=sys.stderr)
        # POSIX root check only; Windows admin detection is skipped (getuid absent).
        if not sys.platform.startswith("win") and getattr(os, "getuid", lambda: -1)() == 0:
            print(
                "refusing to run in safe mode as root — use a non-root user, or --mode yolo knowingly",
                file=sys.stderr,
            )
            return 1
        mode = "default"
    else:
        mode = args.mode

    # resume: --continue feeds the history back as context and keeps the same
    # session file; --resume shows a summary then starts a fresh session.
    cwd = args.cwd.resolve()
    root = session_root()
    resumed = None
    continue_mode = False
    if args.session_id:
        resumed = find_session(root, args.session_id)
        if resumed is None:
            print(f"session not found: {args.session_id}", file=sys.stderr)
            return 1
    elif args.continue_flag:
        resumed = most_recent_session(root)
        if resumed is None:
            print("no sessions to continue", file=sys.stderr)
            return 1
        continue_mode = True
    elif args.resume:
        resumed = most_recent_session(root)
        if resumed is None:
            print("no sessions to resume", file=sys.stderr)
            return 1

    project_key = None
    if resumed is not None and resumed.parent != root:
        project_key = resumed.parent.name

    history = None
    session = None
    if resumed is not None:
        if continue_mode:
            # --continue: load prior turns as context and keep appending to
            # the same session file (chains across runs). 12 §4.4/§5:--lane
            # 选分支 —— 线性视图换 lane 并重置续写游标;未知 lane → 报错退出。
            session = Session(resumed.stem, root, project_key=project_key)
            try:
                history = session.load_lane(args.lane) if args.lane else session.load()
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(_c(f"Continuing session {resumed.stem} ({len(history)} messages)", CYAN), file=sys.stderr)
            _print_interrupt_notice(session)  # §7.3 中断恢复提示(注意类三段式)
        else:
            # --resume/--session-id:12 §4.5 branch_summary 摘要注入(沿目标
            # lane 找最近摘要,leaf 必须落在该 lane 链上,跨 lane 过滤);未命中
            # → 07 旧逻辑(最后 10 条渲染)。resume 是新会话:history 只注入,
            # session 不传 build_loop(仍新建文件)。
            resumed_session = Session(resumed.stem, root, project_key=project_key)
            try:
                history = resume_inject_history(resumed_session, args.lane)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            if history is not None:
                print(f"[resuming {resumed.stem}: {len(history)} message(s), branch summary]")
            else:
                _print_history_summary(resumed, root)

    loop = build_loop(
        cwd=cwd,
        mode=mode,
        model=args.model,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget_usd,
        request_permission=None if prompt else _interactive_permission(),
        project_key=project_key,
        system_prompt=system_prompt,
        session=session,
        history=history,
    )
    apply_tool_filter(loop, args.allowedTools, args.disallowedTools)
    _warn_if_no_api_key(loop)

    if prompt:
        # single-shot: no UI for permission asks — denials go back to the model
        _install_single_shot_sigint(loop)  # CC-11: Ctrl+C aborts + exits 130
        if args.output_format == "json":
            # CC submitMessage 外壳:无渲染,收集 RunSummary
            summary = asyncio.run(AgentSession(loop).submit(prompt))
        else:
            summary = asyncio.run(
                run_single_turn(
                    loop,
                    prompt,
                    show_thinking=args.show_thinking,
                    render=True,
                )
            )
        if args.output_format == "json":
            print(json.dumps(summary.to_dict(), ensure_ascii=False))
        if summary.budget_exceeded:
            print("Error: Exceeded USD budget", file=sys.stderr)
            return 1
        if summary.max_turns_exceeded:
            print("Error: Exceeded max turns", file=sys.stderr)
            return 1
        return 1 if summary.is_error else 0
    if not args.verbose and args.debug is None:
        # 交互模式:hook 成功输出默认可见(§4.1 stderr 是给人类看的摘要,
        # 测试/排查 hook 的刚需);仅开 codesage.hooks 一个 logger,其他
        # INFO 日志仍默认关闭(--verbose 全局开)
        _enable_interactive_hook_logging()
    asyncio.run(repl_loop(loop, cwd=cwd, show_thinking=args.show_thinking))
    return 0


def _enable_interactive_hook_logging() -> None:
    """交互模式默认装配:codesage.hooks → INFO + stderr handler,幂等。"""
    h = logging.getLogger("codesage.hooks")
    h.setLevel(logging.INFO)
    if not h.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        h.addHandler(handler)


def resume_inject_history(
    session: Session, lane: str | None = None
) -> list[SessionMessage] | None:
    """12 §4.5 --resume 摘要注入(可测纯逻辑):沿目标 lane(缺省活跃,--lane
    指定)按文件序倒序找最近 branch_summary,其 leaf 必须落在该 lane 链上
    (跨 lane 过滤 —— 别的分支的摘要跳过,继续往前找);命中 → [摘要 boundary
    消息(10 的 is_compaction_summary 模式,summary_message)+ leaf 链往前
    2 条 user 消息(保留清单 #14)];未命中 → None(调用方走 07 旧逻辑)。
    未知 lane 抛 ValueError(CLI 捕获报错)。"""
    entries = session.entries
    if lane is not None and lane not in lane_names(entries):
        raise ValueError(f"lane not found: {lane}")
    chain = linear_messages(entries, lane)
    chain_ids = {m.uuid for m in chain}
    for entry in reversed(entries):
        if entry.type != "branch_summary":
            continue
        if entry.data.get("leaf") not in chain_ids:
            continue  # 别的分支的摘要(leaf ∉ 目标 lane 链):跳过
        # 真实 user 输入 = 字符串内容且非摘要载体(排除 tool_result 载体/
        # 既存压缩摘要);保留最近 2 条作为上下文起点
        users = [
            m
            for m in chain
            if m.role == "user"
            and isinstance(m.content, str)
            and not m.is_compaction_summary
        ]
        return [summary_message(entry.data.get("content", ""))] + users[-2:]
    return None


def _print_interrupt_notice(session: Session) -> None:
    """§7.3 --continue 中断恢复提示(§1.4.1 注意类三段式:[!] 前缀 + 事实 +
    entry 序号 + 动作建议):启动时检测活跃 lane 末段未完成操作
    (find_open_operations),命中 → 打印提示;未命中 → 维持既有
    "Continuing session ..." 输出。只提示不重放(工具副作用不可重放,
    重放是 13 子代理的职责)。"""
    entries = session.entries
    ops = find_open_operations(entries)
    if not ops:
        return
    op = ops[-1]
    num = next((n for n, e in numbered_entries(entries) if e.uuid == op.uuid), None)
    tool, args_summary = op.data.get("tool"), op.data.get("args_summary")
    label = f'{tool}("{args_summary}")' if tool else op.data.get("kind") or op.type
    print(f"[!] 上次运行中断于工具调用: {label}(entry {num}) —— 从该点继续", file=sys.stderr)


def _print_history_summary(path: Path, root: Path, limit: int = 10) -> None:
    """Resume: print the last *limit* messages of a session, then a new turn starts."""
    project_key = path.parent.name if path.parent != root else None
    messages = Session(path.stem, root, project_key).load()
    print(f"[resuming {path.stem}: {len(messages)} message(s), showing last {min(limit, len(messages))}]")
    for message in messages[-limit:]:
        render_message(message)


def _warn_if_no_api_key(loop) -> None:
    """Print a one-time setup hint when the main profile has no API key.

    Without a key every call fails with an opaque provider error; the hint
    points at the two supported configuration paths. Only fires when the key
    is actually missing — configured setups stay silent.
    """
    try:
        profile = loop.client.resolve_profile("main")
    except Exception:
        return
    if profile.get_api_key():
        return
    print(
        "[hint] 未配置模型 API key — 所有调用将失败。两种配置方式:\n"
        "  1) 环境变量: CODESAGE_MODEL=... CODESAGE_BASE_URL=... CODESAGE_API_KEY_ENV=...\n"
        "  2) 全局配置: ~/.codesage/config.json 的 model_profiles.main(api_key 或 api_key_env)",
        file=sys.stderr,
    )


def _interactive_permission():
    local_settings = paths.local_settings_path()
    return lambda decision, tool, tool_input: request_permission(
        decision, tool, tool_input, local_settings_path=local_settings
    )


if __name__ == "__main__":
    sys.exit(main())
