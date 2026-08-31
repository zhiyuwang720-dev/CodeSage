"""spec §6 test_plain_diff_cli: stdin diff → 输出合法 [{path,line,body}] JSON。

阶段 02 前占位审查器返回空数组, 合法(§6 第 1 行)。
全离线: 不触网、不依赖数据库。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]

SAMPLE_DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,3 +1,5 @@
 import os
+import json
+
+VALUE = 42
"""


def run_cli(*args: str, stdin_text: str | None = None, cwd: Path = BACKEND_ROOT) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    env["CODESAGE_PR_DATA_ROOT"] = str(BACKEND_ROOT / ".auditai-cli-test")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        env=env,
        timeout=120,
        check=False,
    )


def test_stdin_diff_outputs_valid_json():
    proc = run_cli("review", "--diff-file", "-", "--output", "json", stdin_text=SAMPLE_DIFF)
    assert proc.returncode == 0, proc.stderr
    comments = json.loads(proc.stdout)
    assert isinstance(comments, list)
    for c in comments:  # 阶段 01 为空数组; 非空时必须符合注入格式
        assert set(c) >= {"path", "line", "body"}


def test_diff_file_input(tmp_path):
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(SAMPLE_DIFF, encoding="utf-8")
    proc = run_cli("review", "--diff-file", str(diff_file), "--output", "json")
    assert proc.returncode == 0, proc.stderr
    assert isinstance(json.loads(proc.stdout), list)


def test_text_output(tmp_path):
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(SAMPLE_DIFF, encoding="utf-8")
    proc = run_cli("review", "--diff-file", str(diff_file), "--output", "text")
    assert proc.returncode == 0, proc.stderr
    assert "review_id" in proc.stdout


def test_empty_stdin_fails_cleanly():
    proc = run_cli("review", "--diff-file", "-", stdin_text="")
    assert proc.returncode == 2
    assert "stdin 为空" in proc.stderr
