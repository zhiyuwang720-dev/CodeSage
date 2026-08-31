"""pr_review 测试夹具: 本地 git fixture 仓库(spec §6 test_importer 等共用)。"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _isolate_pr_data_root(tmp_path, monkeypatch):
    """每个测试独立 .auditai 根, 不污染工作区(路径函数请求时读 env)。"""
    monkeypatch.setenv("CODESAGE_PR_DATA_ROOT", str(tmp_path / "auditai"))
    return tmp_path / "auditai"


def git(cwd, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout


BASE_UTILS = "def add(a, b):\n    return a + b\n"
BASE_SERVICE = "from utils import add\n\n\ndef total(xs):\n    return add(sum(xs), 0)\n"
BASE_TEST = "from utils import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
HEAD_UTILS = (
    "import json\n\n\ndef add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
)


@pytest.fixture()
def fixture_repo(tmp_path):
    """base: utils.py + 调用方 service.py + 测试 tests/test_utils.py;
    head(feature): utils.py 增加 mul 与 import json(检验 diff 引用分析)。"""
    repo = tmp_path / "fixture"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "tester@example.com")
    git(repo, "config", "user.name", "tester")
    (repo / "utils.py").write_text(BASE_UTILS, encoding="utf-8")
    (repo / "service.py").write_text(BASE_SERVICE, encoding="utf-8")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_utils.py").write_text(BASE_TEST, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "chore: initial base")
    base_sha = git(repo, "rev-parse", "HEAD").strip()

    git(repo, "checkout", "-b", "feature/mul")
    (repo / "utils.py").write_text(HEAD_UTILS, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "feat(utils): add mul helper")
    head_sha = git(repo, "rev-parse", "HEAD").strip()

    # 主分支再加一个提交, 使 base..head 区间有明确起点
    git(repo, "checkout", "main")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "docs: readme")
    return SimpleNamespace(path=repo, base_sha=base_sha, head_sha=head_sha)
