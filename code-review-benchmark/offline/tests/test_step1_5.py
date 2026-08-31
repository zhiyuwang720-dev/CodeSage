"""spec §6 test_inject_format / test_inject_incremental: step1.5 注入脚本(离线)。

用假数据 + monkeypatch(不联网、不依赖真实 LLM), 验证:
- 注入后 benchmark_data.json 结构合法(与既有 tool 条目同构, step2 可无错读取)
- 增量语义: 二次运行跳过已注入 tool; --force 覆盖
- diff 缓存: 首次 fetch 落盘, 二次命中缓存
- 缺失率 >10% → 退出码 2(spec §7)
"""
import json
import sys
from pathlib import Path

import pytest

offline_root = Path(__file__).resolve().parents[1]
if str(offline_root) not in sys.path:
    sys.path.insert(0, str(offline_root))

from code_review_benchmark import step1_5_inject_codesage as inject_mod  # noqa: E402


def _fake_benchmark(tmp_path: Path, n: int = 3) -> tuple[Path, dict]:
    data = {}
    for i in range(1, n + 1):
        data[f"https://github.com/sentry/sentry/pull/{100 + i}"] = {
            "original_url": f"https://github.com/sentry/sentry/pull/{100 + i}",
            "pr_title": f"PR {i}",
            "source_repo": "sentry/sentry",
            "golden_comments": [{"comment": "问题", "severity": "High", "category": "bug"}],
            "reviews": [
                {"tool": "claude-code", "pr_url": f"https://github.com/sentry/sentry/pull/{100 + i}",
                 "repo_name": "sentry/sentry",
                 "review_comments": [{"path": "a.py", "line": "3", "body": "问题", "created_at": "2026-01-01T00:00:00Z"}]},
            ],
        }
    path = tmp_path / "benchmark_data.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def _comments(n: int = 2) -> list[dict]:
    return [
        {"path": "app.py", "line": 3, "body": "[Security] eval 注入风险", "severity": "critical", "category": "security"},
        {"path": "app.py", "line": 5, "body": "[Quality] except 吞异常", "severity": "medium", "category": "bug"},
    ][:n]


def test_inject_format_structure_parity(tmp_path):
    """注入条目与既有 tool 条目同构(review_comments 键集合一致)→ step2 可读。"""
    data_path, _ = _fake_benchmark(tmp_path)
    data = inject_mod.load_data(data_path)
    pr_url = next(iter(data))
    entry = inject_mod.to_review_entry(pr_url, _comments())
    assert inject_mod.inject(data, entry)
    inject_mod.save_data(data_path, data)

    reloaded = inject_mod.load_data(data_path)
    injected = next(r for r in reloaded[pr_url]["reviews"] if r["tool"] == "codesage")
    assert injected["pr_url"] == pr_url
    assert injected["repo_name"] == "sentry/sentry"
    assert set(injected.keys()) == {"tool", "pr_url", "repo_name", "review_comments"}
    existing = reloaded[pr_url]["reviews"][0]
    for comment in injected["review_comments"]:
        assert set(comment.keys()) == set(existing["review_comments"][0].keys())
        assert comment["body"].startswith("[")
        assert isinstance(comment["line"], str) and comment["line"]


def test_inject_incremental_skip_and_force(tmp_path):
    data_path, data = _fake_benchmark(tmp_path)
    pr_url = next(iter(data))
    entry = inject_mod.to_review_entry(pr_url, _comments())
    assert inject_mod.inject(data, entry) is True
    assert inject_mod.inject(data, inject_mod.to_review_entry(pr_url, _comments(1))) is False, "二次运行跳过"
    assert len([r for r in data[pr_url]["reviews"] if r["tool"] == "codesage"]) == 1

    assert inject_mod.inject(data, inject_mod.to_review_entry(pr_url, _comments(1)), force=True) is True
    codesage = [r for r in data[pr_url]["reviews"] if r["tool"] == "codesage"]
    assert len(codesage) == 1 and len(codesage[0]["review_comments"]) == 1, "--force 覆盖不重复"


def test_results_file_injection(tmp_path):
    """预计算模式: 一次写入多个 PR; 已存在的跳过。"""
    data_path, _ = _fake_benchmark(tmp_path, n=3)
    results = [
        {"pr_url": f"https://github.com/sentry/sentry/pull/{100 + i}", "review_comments": _comments()}
        for i in (1, 2)
    ]
    data = inject_mod.load_data(data_path)
    written, skipped = inject_mod.inject_results_file(data, results)
    written2, skipped2 = inject_mod.inject_results_file(data, results)
    assert (written, skipped) == (2, 0)
    assert (written2, skipped2) == (0, 2)


def test_cached_diff_offline_second_call(tmp_path, monkeypatch):
    """diff 缓存: 首次 fetch 落盘, 二次不再联网(spec §7 边界)。"""
    calls = {"n": 0}

    def fake_fetch(pr_url: str) -> str:
        calls["n"] += 1
        return "diff --git a/x.py b/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n+x = 1\n"

    monkeypatch.setattr(inject_mod, "fetch_diff", fake_fetch)
    url = "https://github.com/sentry/sentry/pull/101"
    first = inject_mod.cached_diff(tmp_path / "cache", url)
    second = inject_mod.cached_diff(tmp_path / "cache", url)
    assert calls["n"] == 1
    assert first == second and "+x = 1" in second


def test_missing_rate_over_ten_percent_exit_two(tmp_path, monkeypatch):
    """单 PR 崩溃不阻断; 缺失率 >10% → 退出码 2(spec §6/§7)。"""
    data_path, _ = _fake_benchmark(tmp_path, n=5)  # 5 个 PR, 1 个失败 = 20% > 10%
    monkeypatch.setattr(
        inject_mod, "cached_diff",
        lambda cache_dir, url, **kw: "diff --git a/x.py b/x.py\n",
    )
    order = [f"https://github.com/sentry/sentry/pull/{100 + i}" for i in range(1, 6)]

    def run_cli_by_url(diff_text, *, backend_root, engine="rules", timeout=300, _order=order, _state={"i": -1}):
        _state["i"] += 1
        if _order[_state["i"]].endswith("103"):
            raise RuntimeError("模拟单 PR 崩溃")
        return _comments()

    monkeypatch.setattr(inject_mod, "run_cli", run_cli_by_url)
    code = inject_mod.main([
        "--benchmark-data", str(data_path),
        "--backend-root", str(tmp_path),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    assert code == 2
    data = inject_mod.load_data(data_path)
    injected = [u for u, v in data.items() if any(r.get("tool") == "codesage" for r in v["reviews"])]
    assert len(injected) == 4, "其余 4 个 PR 正常写入"


def test_pr_diff_integration_rules_engine(tmp_path, monkeypatch):
    """真 CLI(纯规则引擎)单 PR 全链路: fetch(假) → run_cli → 注入结构合法。"""
    data_path, _ = _fake_benchmark(tmp_path, n=1)
    diff_text = (
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        "@@ -1,2 +1,3 @@\n import os\n+result = eval(request.args['expr'])\n"
    )
    monkeypatch.setattr(inject_mod, "cached_diff", lambda cache_dir, url, **kw: diff_text)
    code = inject_mod.main([
        "--benchmark-data", str(data_path),
        "--backend-root", "E:/Mac/CodeSage/backend",
        "--cache-dir", str(tmp_path / "cache"),
    ])
    assert code == 0
    data = inject_mod.load_data(data_path)
    entry = next(r for r in data[next(iter(data))]["reviews"] if r["tool"] == "codesage")
    assert entry["review_comments"], "规则引擎产出真实评论"
    assert any("eval" in c["body"] for c in entry["review_comments"])
