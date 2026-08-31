"""CodeSage PR 审查 CLI(阶段 01 §4.5, benchmark 注入通道)。

用法:
  python -m app.cli review --pr-url https://github.com/o/r/pull/1 --output json
  python -m app.cli review --diff-file pr.diff --context-file ctx.md --output json
  cat pr.diff | python -m app.cli review --output json

输出: [{path, line, body, severity, category}](阶段 01 占位审查器返回空数组, 合法)。
全离线可用: plain-diff + --context-file 通道不触网(§7 CI 环境无网络)。
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.subcommand != "review":
        print(f"未知子命令: {args.subcommand}", file=sys.stderr)
        return 2

    diff_text: str | None = None
    if args.pr_url:
        pass  # diff+上下文模式: diff 由 provider/importer 获取
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

    try:
        if args.engine == "runtime":
            import asyncio

            result = asyncio.run(
                run_review_pipeline_async(
                    pr_url=args.pr_url,
                    diff_text=diff_text,
                    user_context=user_context,
                    command=args.command,
                    options=options,
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
