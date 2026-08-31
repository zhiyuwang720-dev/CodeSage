"""spec §6 test_rules_engine: 规则命中/误报样例(移植 evoagent 规则 + 扩充)。"""
from app.services.pr_review.rules import RULES, run_rules

DIFF = """diff --git a/worker.py b/worker.py
--- a/worker.py
+++ b/worker.py
@@ -1,3 +1,10 @@
 import os
+result = eval(request.args["expr"])
+subprocess.run(cmd, shell=True)
+db_password = "super-secret-123"
+cursor.execute(f"SELECT * FROM users WHERE name = '{name}'")
+try:
+    risky()
+except:
+    pass
+print("debug here")
+time.sleep(5)
"""


def test_all_major_rules_hit():
    findings = run_rules(DIFF)
    rule_ids = {f.rule_id for f in findings}
    assert {
        "SEC-EVAL", "SEC-SUBPROCESS-SHELL", "SEC-HARDCODED-SECRET",
        "SEC-SQL-CONCAT", "REL-EMPTY-EXCEPT", "REL-DEBUG-PRINT", "CONC-SLEEP-LOCK",
    } <= rule_ids


def test_findings_are_structurally_valid():
    for finding in run_rules(DIFF):
        assert finding.source == "rules"
        assert finding.severity in {"low", "medium", "high", "critical"}
        assert finding.line_start >= 1
        assert finding.confidence >= 0.9, "确定性规则高置信"


def test_context_lines_not_flagged():
    """只审新增行: 未改动的上下文行不产生评论。"""
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n import os\n-print('old debug')\n+pass\n"
    assert run_rules(diff) == []


def test_lock_and_generated_files_skipped():
    diff = "diff --git a/package-lock.json b/package-lock.json\n--- a/package-lock.json\n+++ b/package-lock.json\n@@ -1,2 +1,3 @@\n x\n+password = 'secret-999'\n"
    assert run_rules(diff) == []


def test_rule_registry_expandable():
    """规则表可扩充: 追加自定义规则即刻生效。"""
    import re

    from app.services.pr_review.rules import Rule

    custom = Rule(
        rule_id="TEAM-NO-GOTO", severity="low", category="bug",
        pattern=re.compile(r"\bgoto\b"), title="禁用 goto",
        description="团队规范", suggestion="重构控制流", test_hint="x",
    )
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n x\n+goto end\n"
    assert [f.rule_id for f in run_rules(diff, rules=[custom])] == ["TEAM-NO-GOTO"]
    assert len(RULES) >= 11, "基础 6 条 + 扩充规则"


def test_rules_via_pipeline_rules_engine(tmp_path):
    """CLI 默认引擎(纯规则, 全离线)端到端: diff 进 → 评论 JSON 出。"""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend_root = Path(__file__).resolve().parents[2]
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(DIFF, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(backend_root)
    env["CODESAGE_PR_DATA_ROOT"] = str(tmp_path / "auditai")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "review", "--diff-file", str(diff_file), "--output", "json"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(backend_root), env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    comments = json.loads(proc.stdout)
    assert comments, "规则引擎产出真实评论"
    assert all(c["path"] == "worker.py" for c in comments)
    assert any("eval" in c["body"] or "动态" in c["body"] for c in comments)
