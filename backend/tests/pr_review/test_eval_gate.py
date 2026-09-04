"""spec §6 test_eval_gate: 门禁规则 + 回归快照 + 分视角归约 + 已知退化区分力。

数据契约 = benchmark step3 evaluations.json; 全部合成数据, 离线确定性。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.pr_review.eval_gate import (
    check_gate,
    compute_metrics,
    perspective_breakdown,
    snapshot_diff,
    source_of_comment,
)


def _pr(tp: int, fp: int, fn: int, *, high_fn: int = 0, high_tp: int = 0) -> dict:
    """合成单 PR 评测条目(结构与 step3 输出对齐)。

    高危 golden 命名为 golden-0..N, TP 与 FN 共用同一命名空间,
    便于构造"TP→FN 退化"对照。
    """
    entry: dict = {
        "skipped": False,
        "tp": tp, "fp": fp, "fn": fn,
        "total_candidates": tp + fp, "total_golden": tp + fn,
        "true_positives": [
            {"golden_comment": f"golden-{i}", "severity": "high", "category": "security",
             "matched_candidate": "[Security] eval 注入"}
            for i in range(high_tp)
        ],
        "false_positives": [{"candidate": "[Quality] 变量命名不佳"} for _ in range(fp)],
        "false_negatives": [],
    }
    low_fn = max(fn - high_fn, 0)
    entry["false_negatives"] = (
        [{"golden_comment": f"golden-{i}", "severity": "high", "category": "bug"} for i in range(high_fn)]
        + [{"golden_comment": f"low-miss-{i}", "severity": "low", "category": "bug"} for i in range(low_fn)]
    )
    return entry


def _evals(*entries: dict) -> dict:
    return {f"https://github.com/o/r/pull/{i + 1}": e for i, e in enumerate(entries)}


def _baseline() -> dict:
    """基线: 3 PR, TP=12, FP=10, FN=8 → precision .545, recall .600; 含 2 高危 TP。"""
    return _evals(_pr(tp=5, fp=3, fn=2, high_tp=1), _pr(tp=4, fp=4, fn=3, high_tp=1), _pr(tp=3, fp=3, fn=3))


# ---------- compute_metrics ----------

def test_compute_metrics_aggregates():
    m = compute_metrics(_baseline())
    assert (m.tp, m.fp, m.fn) == (12, 10, 8)
    assert m.precision == pytest.approx(12 / 22)
    assert m.recall == pytest.approx(12 / 20)
    assert m.high_severity_fn == []


def test_skipped_entries_excluded():
    evals = _evals(_pr(tp=2, fp=1, fn=1), _pr(tp=100, fp=100, fn=100))
    evals["https://github.com/o/r/pull/2"]["skipped"] = True
    m = compute_metrics(evals)
    assert m.tp == 2 and m.fp == 1


# ---------- check_gate ----------

def test_gate_recall_drop_six_percent_blocks():
    """recall -6% → 门禁红(spec §6)。"""
    current = _evals(_pr(tp=5, fp=3, fn=2, high_tp=1), _pr(tp=4, fp=4, fn=3, high_tp=1), _pr(tp=3, fp=3, fn=5))
    result = check_gate(_baseline(), current)
    assert not result.passed
    assert any("recall" in r for r in result.reasons)


def test_gate_fp_growth_within_threshold_passes():
    """新增 FP 8% → 放行(spec §6)。"""
    current = _evals(_pr(tp=5, fp=3, fn=2, high_tp=1), _pr(tp=4, fp=5, fn=3, high_tp=1), _pr(tp=3, fp=3, fn=3))
    result = check_gate(_baseline(), current)
    assert result.passed, result.reasons


def test_gate_fp_growth_over_ten_percent_blocks():
    current = _evals(_pr(tp=5, fp=6, fn=2, high_tp=1), _pr(tp=4, fp=5, fn=3, high_tp=1), _pr(tp=3, fp=4, fn=3))
    result = check_gate(_baseline(), current)
    assert not result.passed
    assert any("FP" in r for r in result.reasons)


def test_gate_high_severity_regression_blocks():
    """高危 golden TP→FN → 直接阻塞(spec §3.3)。"""
    baseline = _evals(_pr(tp=3, fp=2, fn=1, high_tp=2))
    current = _evals(_pr(tp=3, fp=2, fn=1, high_tp=0, high_fn=2))
    result = check_gate(baseline, current)
    assert not result.passed
    assert any("高危" in r for r in result.reasons)


def test_gate_identical_results_pass():
    baseline = _baseline()
    assert check_gate(baseline, json.loads(json.dumps(baseline))).passed


# ---------- snapshot_diff ----------

def test_snapshot_diff_deltas():
    baseline = _baseline()
    current = _evals(_pr(tp=6, fp=3, fn=1, high_tp=1), _pr(tp=4, fp=4, fn=3, high_tp=1), _pr(tp=3, fp=3, fn=3))
    report = snapshot_diff(baseline, current)
    assert report["overall"]["delta"]["tp"] == 1
    assert report["overall"]["delta"]["fn"] == -1
    assert report["changed_prs"] == 1
    first = next(iter(report["prs"].values()))
    assert first["delta"] == {"tp": 1, "fp": 0, "fn": -1}
    assert report["high_severity_regressions"] == []


def test_snapshot_detects_high_severity_regression():
    baseline = _evals(_pr(tp=2, fp=1, fn=0, high_tp=2))
    current = _evals(_pr(tp=2, fp=1, fn=0, high_tp=1, high_fn=1))
    report = snapshot_diff(baseline, current)
    assert len(report["high_severity_regressions"]) == 1


# ---------- perspective_breakdown ----------

def test_perspective_breakdown_by_source_prefix():
    """TP/FP 按 [Label] 前缀归属正确(spec §6 test_perspective_breakdown)。"""
    evals = {
        "pr1": {
            "skipped": False, "tp": 2, "fp": 1, "fn": 1,
            "true_positives": [
                {"golden_comment": "g1", "severity": "high", "category": "security",
                 "matched_candidate": "[Security] eval 注入风险"},
                {"golden_comment": "g2", "severity": "medium", "category": "bug",
                 "matched_candidate": "[Quality] except 吞异常"},
            ],
            "false_positives": [
                {"candidate": "[Quality] 变量命名"},
                {"candidate": "无前缀候选"},
            ],
            "false_negatives": [{"golden_comment": "g3", "severity": "low", "category": "bug"}],
        },
    }
    breakdown = perspective_breakdown(evals)
    assert breakdown["security"]["tp"] == 1 and breakdown["security"]["fp"] == 0
    assert breakdown["quality"]["tp"] == 1 and breakdown["quality"]["fp"] == 1
    assert breakdown["unattributed"]["fp"] == 1 and breakdown["unattributed"]["fn"] == 1
    assert breakdown["security"]["precision"] == 1.0
    assert breakdown["quality"]["precision"] == pytest.approx(0.5)


def test_source_of_comment_parses_prefix():
    assert source_of_comment("[Security] eval 注入") == "security"
    assert source_of_comment("  [Architecture] 分层违规") == "architecture"
    assert source_of_comment("无前缀") is None
    assert source_of_comment("") is None


def test_cli_body_prefix_convention():
    """CLI 输出 body 以 [Rules] 等前缀开头(评测归因约定, spec §7.105)。"""
    from app.services.pr_review.synthesizer import finding_to_comment
    from app.services.contracts.final_review_contract import ReviewFinding

    finding = ReviewFinding.model_validate(dict(
        rule_id="SEC-EVAL", severity="critical", category="security",
        title="动态执行", description="d", file_path="a.py", line_start=2, line_end=2,
        confidence=0.9, needs_verification=False, verdict="confirmed", source="rules",
    ))
    comment = finding_to_comment(finding)
    assert comment["body"].startswith("[Rules] ")
    assert comment["body"].startswith("[Rules] **动态执行**")


# ---------- test_known_degradation ----------

def test_known_degradation_gate_catches_rules_off():
    """spec §6 test_known_degradation: 已知退化(禁用规则层)必须被门禁拦下。

    合成对照: 基线 20 golden/12 TP(recall .60); 禁规则层后高危+确定性类 golden
    大量丢失 → recall .40(降 20%, 远超 5% 阈值)→ 门禁红 → 证明评测有区分力。
    """
    baseline = _baseline()
    degraded = _evals(
        _pr(tp=3, fp=3, fn=4, high_tp=1, high_fn=0),
        _pr(tp=2, fp=4, fn=5, high_tp=0, high_fn=1),
        _pr(tp=1, fp=2, fn=5, high_tp=0, high_fn=0),
    )
    base_recall = compute_metrics(baseline).recall
    degraded_recall = compute_metrics(degraded).recall
    assert base_recall - degraded_recall >= 0.05, "退化幅度应显著(≥5%)"
    result = check_gate(baseline, degraded)
    assert not result.passed, "门禁必须拦下已知退化"
    assert any("recall" in r for r in result.reasons)


def test_step3_5_snapshot_cli_smoke(tmp_path):
    """step3_5 脚本端到端: JSON/Markdown 两种报告可生成(benchmark 侧桥接)。"""
    offline = Path("E:/Mac/CodeSage/code-review-benchmark/offline")
    baseline_path = tmp_path / "baseline.json"
    current_path = tmp_path / "current.json"
    baseline_path.write_text(json.dumps(_baseline()), encoding="utf-8")
    current = _evals(_pr(tp=5, fp=3, fn=2, high_tp=1), _pr(tp=4, fp=4, fn=3, high_tp=1), _pr(tp=3, fp=3, fn=5))
    current_path.write_text(json.dumps(current), encoding="utf-8")
    for fmt in ("json", "md"):
        out = tmp_path / f"report.{fmt}"
        proc = subprocess.run(
            [sys.executable, "-m", "code_review_benchmark.step3_5_snapshot",
             "--baseline", str(baseline_path), "--current", str(current_path),
             "--output", str(out), "--format", fmt],
            capture_output=True, text=True, cwd=str(offline), env={"PYTHONPATH": str(offline), "PYTHONIOENCODING": "utf-8"},
        )
        assert proc.returncode == 0, proc.stderr
        assert out.is_file()
        if fmt == "json":
            payload = json.loads(out.read_text(encoding="utf-8"))
            assert payload["overall"]["delta"]["fn"] == 2
        else:
            assert "评测回归快照" in out.read_text(encoding="utf-8")
