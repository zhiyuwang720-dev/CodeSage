"""Step 1.5 — 把 CodeSage 产品评论注入 benchmark_data.json(阶段 03 官方评测通道)。

不 fork benchmark 评分管线: 产品输出按 {tool, pr_url, repo_name, review_comments} 结构
追加为新 tool 条目, 之后直接跑 step2(LLM 提取)→step2.5→step3(LLM judge)。

用法:
    # 全量 50 PR(rules 引擎, 离线, diff 经缓存)
    python -m code_review_benchmark.step1_5_inject_codesage \
        --backend-root E:/Mac/CodeSage/backend \
        --benchmark-data ../results/benchmark_data.json

    # 增量重跑(跳过已有 codesage 条目的 PR; --force 覆盖)
    ... --limit 5 --force

    # 预计算模式: 直接注入已有 CLI 输出(JSON 数组 [{pr_url, review_comments}])
    ... --results-file codesage_results.json

评论契约(spec 03 §4.1): {path, line(head 行号), body}; body 首段带 [Security] 等
视角前缀(阶段 02 synthesizer.finding_to_comment 约定), 供 step3_5 分视角归因。

设计: 单 PR 失败不阻断(spec §7), 记 error 列表; 缺失率 >10% 退出码 2。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TOOL = "codesage"
_GITHUB_URL_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")


def load_data(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_data(path: Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def repo_name_of(pr_url: str) -> str:
    match = _GITHUB_URL_RE.search(pr_url or "")
    return f"{match['owner']}/{match['repo']}" if match else ""


def fetch_diff(pr_url: str, timeout: int = 60) -> str:
    """公开仓库 PR diff(patch-diff 端点, 无需 token); 失败抛异常由调用方记账。"""
    match = _GITHUB_URL_RE.search(pr_url or "")
    if not match:
        raise ValueError(f"无法解析 GitHub PR URL: {pr_url!r}")
    url = (
        f"https://patch-diff.githubusercontent.com/raw/"
        f"{match['owner']}/{match['repo']}/pull/{match['number']}.diff"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "codesage-eval"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (固定 https 域)
        return response.read().decode("utf-8", errors="replace")


def cached_diff(cache_dir: Path, pr_url: str, *, fetch=None) -> str:
    """diff 落盘缓存(spec §7: 首次联网, 之后离线可跑)。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    match = _GITHUB_URL_RE.search(pr_url or "")
    key = f"{match['owner']}_{match['repo']}_{match['number']}.diff" if match else "unknown.diff"
    cache_file = cache_dir / key
    if cache_file.is_file():
        return cache_file.read_text(encoding="utf-8", errors="replace")
    getter = fetch or fetch_diff
    diff_text = getter(pr_url)
    cache_file.write_text(diff_text, encoding="utf-8")
    return diff_text


def run_cli(
    diff_text: str,
    *,
    backend_root: Path,
    engine: str = "rules",
    timeout: int = 300,
    min_severity: str | None = None,
    max_turns: int | None = None,
) -> list[dict]:
    """调产品 CLI(阶段 01/02 契约): diff 进 → [{path, line, body, severity, category}] 出。"""
    import os
    import tempfile

    backend_root = Path(backend_root).resolve()
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8") as handle:
        handle.write(diff_text)
        diff_path = handle.name
    try:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(backend_root) + os.pathsep + env.get("PYTHONPATH", "")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        cmd = [sys.executable, "-m", "app.cli", "review", "--diff-file", diff_path,
               "--output", "json", "--engine", engine]
        if min_severity:
            cmd += ["--min-severity", min_severity]
        if max_turns:
            cmd += ["--max-turns", str(max_turns)]
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(backend_root), env=env, timeout=timeout,
        )
    finally:
        Path(diff_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"CLI 失败(exit {proc.returncode}): {proc.stderr[-400:]}")
    return json.loads(proc.stdout)


def to_review_entry(
    pr_url: str,
    comments: list[dict],
    *,
    tool: str = DEFAULT_TOOL,
    created_at: str | None = None,
) -> dict:
    """CLI 评论 → benchmark reviews 条目(结构对齐 step1 产物)。"""
    stamp = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    review_comments = [
        {
            "path": str(item.get("path") or ""),
            "line": str(item.get("line") or ""),
            "body": str(item.get("body") or ""),
            "created_at": stamp,
        }
        for item in comments or []
    ]
    return {
        "tool": tool,
        "pr_url": pr_url,
        "repo_name": repo_name_of(pr_url),
        "review_comments": review_comments,
    }


def inject(data: dict, entry: dict, *, force: bool = False) -> bool:
    """写入 benchmark_data(增量: 已有同名 tool 且未 --force → 跳过)。返回是否写入。"""
    reviews = data.setdefault(entry["pr_url"], {}).setdefault("reviews", [])
    if not force and any(r.get("tool") == entry["tool"] for r in reviews):
        return False
    reviews[:] = [r for r in reviews if r.get("tool") != entry["tool"]]
    reviews.append(entry)
    return True


def inject_results_file(
    data: dict,
    results: list[dict],
    *,
    tool: str = DEFAULT_TOOL,
    force: bool = False,
) -> tuple[int, int]:
    """预计算模式: results = [{pr_url, review_comments|comments}]。返回 (写入数, 跳过数)。"""
    written = skipped = 0
    for item in results:
        pr_url = item.get("pr_url")
        if not pr_url or pr_url not in data:
            continue
        comments = item.get("review_comments") or item.get("comments") or []
        entry = to_review_entry(pr_url, comments, tool=tool, created_at=item.get("created_at"))
        if inject(data, entry, force=force):
            written += 1
        else:
            skipped += 1
    return written, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodeSage → benchmark 注入(step 1.5)")
    parser.add_argument("--benchmark-data", required=True, help="benchmark_data.json 路径")
    parser.add_argument("--backend-root", required=True, help="CodeSage backend 目录")
    parser.add_argument("--tool", default=DEFAULT_TOOL)
    parser.add_argument("--engine", default="rules", choices=["rules", "runtime"])
    parser.add_argument("--limit", type=int, help="只处理前 N 个 PR")
    parser.add_argument("--pr-url", action="append", help="只处理指定 PR(可重复)")
    parser.add_argument("--force", action="store_true", help="覆盖已注入条目")
    parser.add_argument("--cache-dir", default="diffs_cache")
    parser.add_argument("--results-file", help="预计算模式: 注入已有 CLI 输出后退出")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--min-severity", default=None, choices=["critical", "high", "medium", "low"],
                        help="综合层最低输出严重度(默认产品 low-noise=high; 评测基线建议 medium)")
    parser.add_argument("--max-turns", type=int, default=None, help="runtime 每视角最大轮数(默认引擎内置)")
    parser.add_argument("--concurrency", type=int, default=1, help="并发 PR 数(限 1-4, spec §4.2)")
    args = parser.parse_args(argv)

    data_path = Path(args.benchmark_data)
    data = load_data(data_path)

    if args.results_file:
        results = json.loads(Path(args.results_file).read_text(encoding="utf-8"))
        written, skipped = inject_results_file(data, results, tool=args.tool, force=args.force)
        save_data(data_path, data)
        print(f"预计算注入: 写入 {written}, 跳过 {skipped}")
        return 0

    targets = list(data.keys())
    if args.pr_url:
        targets = [u for u in targets if u in set(args.pr_url)]
    pending = [
        u for u in targets
        if args.force or not any(r.get("tool") == args.tool for r in data[u].get("reviews", []))
    ]
    if args.limit:
        pending = pending[: args.limit]
    print(f"目标 {len(targets)} 个 PR, 待注入 {len(pending)}(tool={args.tool}, engine={args.engine})")

    cache_dir = Path(args.cache_dir)
    errors: list[str] = []
    written = 0
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    data_lock = threading.Lock()
    counter = {"i": 0}

    def _process(pr_url: str) -> str:
        try:
            diff_text = cached_diff(cache_dir, pr_url)
            comments = run_cli(diff_text, backend_root=Path(args.backend_root),
                               engine=args.engine, timeout=args.timeout,
                               min_severity=args.min_severity, max_turns=args.max_turns)
            with data_lock:
                if inject(data, to_review_entry(pr_url, comments, tool=args.tool)):
                    nonlocal_written[0] += 1
                counter["i"] += 1
                idx = counter["i"]
            print(f"[{idx}/{len(pending)}] OK {pr_url} → {len(comments)} 条评论", flush=True)
            return "ok"
        except Exception as exc:  # noqa: BLE001 — 单 PR 崩溃不阻断(spec §7)
            with data_lock:
                counter["i"] += 1
                idx = counter["i"]
                errors.append(f"{pr_url}: {exc}")
            print(f"[{idx}/{len(pending)}] FAIL {pr_url}: {exc}", flush=True)
            return "fail"

    nonlocal_written = [0]
    concurrency = max(1, min(args.concurrency, 4))
    if concurrency == 1:
        for pr_url in pending:
            _process(pr_url)
            if len(errors) + nonlocal_written[0] and (len(errors) + nonlocal_written[0]) % 5 == 0:
                with data_lock:
                    save_data(data_path, data)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(_process, pr_url) for pr_url in pending]
            for _ in as_completed(futures):
                pass

    with data_lock:
        save_data(data_path, data)
    missing_rate = len(errors) / len(pending) if pending else 0.0
    written = nonlocal_written[0]
    print(f"完成: 写入 {written}/{len(pending)}, 失败 {len(errors)}(缺失率 {missing_rate:.0%})")
    if errors:
        print("\n".join(f"  - {e}" for e in errors))
    if missing_rate > 0.10:
        print("缺失率 >10%, 该次评测无效(spec §7)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
