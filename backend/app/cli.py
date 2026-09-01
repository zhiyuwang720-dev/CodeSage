"""CodeSage PR 审查 CLI(阶段 01 §4.5, benchmark 注入通道)。

用法:
  python -m app.cli review --pr-url https://github.com/o/r/pull/1 --output json
  python -m app.cli review --diff-file pr.diff --context-file ctx.md --output json
  cat pr.diff | python -m app.cli review --output json

输出: [{path, line, body, severity, category}](阶段 01 占位审查器返回空数组, 合法)。
全离线可用: plain-diff + --context-file 通道不触网(§7 CI 环境无网络)。
runtime 引擎长任务默认静默, 交互终端自动开启三屏流式 TUI(--live):
每个视角 Agent 一屏, 顶部显示项目与各视角 sessionID, 逐 token 流式输出(同时
临时关闭 LLM_DISABLE_STREAMING, 真流式); 被管道捕获时保持静默, stdout 始终只
含最终 JSON 数组。--progress 保留为单行进度模式(不回退真流式)。
"""
from __future__ import annotations

import argparse
import json
import sys

from app.services.pr_review.command_router import run_review_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="CodeSage PR 审查 CLI")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    p = sub.add_parser("review", help="审查一个 PR(纯 diff 或 PR URL)")
    p.add_argument("--pr-url", help="GitHub PR URL(diff+上下文模式)")
    p.add_argument("--diff-file", help="统一 diff 文件路径; '-' 表示 stdin(缺省无 --pr-url 时读 stdin)")
    p.add_argument("--context-file", help="用户注入上下文文件(README/架构说明/关注点)")
    p.add_argument("--repo", help="仓库名(repo 覆盖, plain-diff 模式)")
    p.add_argument("--pr-number", type=int, default=None, help="PR 号(plain-diff 模式)")
    p.add_argument("--command", default="review", help="review | describe | ask_line")
    p.add_argument("--engine", choices=["rules", "runtime"], default="rules",
                   help="rules=确定性规则引擎(全离线); runtime=三视角 LLM 编排(需配置 LLM)")
    p.add_argument("--output", choices=["json", "text"], default="json", help="输出格式")
    p.add_argument("--file-budget-bytes", type=int, default=None, help="相关文件字节预算")
    p.add_argument("--min-severity", choices=["critical", "high", "medium", "low"], default=None,
                   help="综合层最低输出严重度(默认 high 低噪原则; benchmark 评测可用 medium)")
    p.add_argument("--max-turns", type=int, default=None, help="runtime 引擎每视角最大轮数(防跑飞)")
    p.add_argument("--progress", action="store_true", default=None,
                   help="runtime 引擎打印进度到 stderr(默认: 交互终端自动开启)")
    p.add_argument("--no-progress", dest="progress", action="store_false",
                   help="强制关闭进度输出(被管道/脚本捕获时默认即关闭)")
    p.add_argument("--live", dest="live", action="store_true", default=None,
                   help="runtime 引擎三屏流式 TUI, 每视角 Agent 一屏(默认: 交互终端自动开启)")
    p.add_argument("--no-live", dest="live", action="store_false",
                   help="强制关闭三屏 TUI(被管道/脚本捕获时默认即关闭)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.subcommand != "review":
        print(f"未知子命令: {args.subcommand}", file=sys.stderr)
        return 2

    diff_text: str | None = None
    if args.pr_url:
        pass  # TODO diff+上下文模式: diff 由 provider/importer 获取
    elif args.diff_file and args.diff_file != "-":
        diff_text = open(args.diff_file, encoding="utf-8", errors="replace").read()
    else:
        diff_text = sys.stdin.read()
        if not diff_text.strip():
            print("错误: stdin 为空, 且未提供 --pr-url / --diff-file", file=sys.stderr)
            return 2

    user_context = None
    if args.context_file:
        user_context = open(args.context_file, encoding="utf-8", errors="replace").read()

    options: dict = {"repo": args.repo, "pr_number": args.pr_number, "engine": args.engine}
    if args.file_budget_bytes:
        options["file_budget_bytes"] = args.file_budget_bytes
    if args.min_severity:
        options["min_severity"] = args.min_severity
    if args.max_turns:
        options["max_turns"] = args.max_turns

    # runtime 引擎长任务默认静默: 交互终端自动开启三屏 TUI(--live), 被管道捕获时静默。
    # event_sink / streaming 都是运行时对象/开关, 不塞进 options 持久化字段——
    # event_sink 作为 kwargs 传入; streaming 在 run_review_pipeline_async 开头从
    # options 抽出(pop), 不会进 build_review_context 的持久化 options。
    event_sink = None
    if args.engine == "runtime":
        tty = sys.stderr.isatty()
        want_live = args.live
        want_progress = args.progress
        if want_live is None and want_progress is None:
            want_live = tty
            want_progress = False
        elif want_live is True:
            want_progress = False
        elif want_progress is True:
            want_live = False
        if want_live:
            from app.services.pr_review.live_sink import LiveReviewSink

            if tty:
                event_sink = LiveReviewSink(sys.stderr)
            else:
                print("警告: 输出被管道捕获, --live 回落为行进度", file=sys.stderr)
                from app.services.pr_review.progress import RuntimeProgressSink

                event_sink = RuntimeProgressSink(sys.stderr)
            # 用户拍板: TUI 下默认真流式(临时关掉网关兼容开关)
            options["streaming"] = True
        elif want_progress:
            from app.services.pr_review.progress import RuntimeProgressSink

            event_sink = RuntimeProgressSink(sys.stderr)

    try:
        if args.engine == "runtime":
            import asyncio

            from app.services.pr_review.command_router import run_review_pipeline_async

            result = asyncio.run(
                run_review_pipeline_async(
                    pr_url=args.pr_url,
                    diff_text=diff_text,
                    user_context=user_context,
                    command=args.command,
                    options=options,
                    event_sink=event_sink,
                )
            )
        else:
            result = run_review_pipeline(
                pr_url=args.pr_url,
                diff_text=diff_text,
                user_context=user_context,
                command=args.command,
                options=options,
            )
    except Exception as exc:  # CLI 友好退出; 不打印堆栈噪音
        print(f"审查失败: {exc}", file=sys.stderr)
        return 1
    finally:
        # 收尾: 停渲染线程、恢复光标, 让 stdout 的 JSON 落在 TUI 下方
        if event_sink is not None:
            event_sink.close()

    if args.output == "json":
        print(json.dumps([c.model_dump() for c in result.comments], ensure_ascii=False, indent=2))
    else:
        print(f"review_id: {result.review_id}")
        print(f"status:    {result.status}")
        print(f"comments:  {len(result.comments)}")
        print(f"context:   {result.context_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
