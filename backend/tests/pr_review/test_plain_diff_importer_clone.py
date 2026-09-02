"""test_plain_diff_importer_clone: import_plain_diff 的 clone_source 克隆路径。

修复 1(纯 diff 审查克隆源码): 给出 clone_source 时克隆源码到 repo_dir(pr_key),
ImportedPr 带 repo_dir + diff_only=False; 克隆失败(私有仓库/网络)降级 diff-only。
全离线: 用本地 git 仓库 fixture, 不触网。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.pr_review.plain_diff_importer import import_plain_diff

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


def _git(repo: Path, *args: str) -> None:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "t")
    env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
    env.setdefault("GIT_COMMITTER_NAME", "t")
    env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture()
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("import os\nprint('hi')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "auditai"
    monkeypatch.setenv("CODESAGE_PR_DATA_ROOT", str(root))
    return root


def test_clone_success(data_root: Path, source_repo: Path):
    imported = import_plain_diff(SAMPLE_DIFF, clone_source=str(source_repo))
    assert imported.diff_only is False
    assert imported.repo_dir is not None
    repo_path = Path(imported.repo_dir)
    assert (repo_path / ".git").is_dir()
    assert (repo_path / "app.py").read_text(encoding="utf-8") == "import os\nprint('hi')\n"
    # 复用既有键: 同 diff 二次导入不重复克隆(.git 已存在)
    imported2 = import_plain_diff(SAMPLE_DIFF, clone_source=str(source_repo))
    assert Path(imported2.repo_dir) == repo_path


def test_clone_failure_falls_back_to_diff_only(data_root: Path, tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    imported = import_plain_diff(SAMPLE_DIFF, clone_source=str(missing))
    # 克隆失败(本地路径不存在 → git 报错)降级 diff-only, 不中断
    assert imported.diff_only is True
    assert imported.repo_dir is None
    # diff 本身仍已落盘可读
    assert imported.diff_text == SAMPLE_DIFF
