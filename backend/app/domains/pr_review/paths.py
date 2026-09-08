"""pr_review 数据目录约定。

默认根: <backend>/.auditai (可用环境变量 CODESAGE_PR_DATA_ROOT 覆盖)。
子目录: repos/ 克隆仓库 · diffs/ diff 落盘 · context/ ReviewContext 产物 ·
reviews/ 审查结果。不触碰 AutoCVE config.py(移植旧代码不动, 环境变量直读)。
"""
import os
from pathlib import Path

_SUBDIRS = ("repos", "diffs", "context", "reviews")


def pr_data_root() -> Path:
    env = os.getenv("CODESAGE_PR_DATA_ROOT")
    if env:
        root = Path(env)
    else:
        root = Path(__file__).resolve().parents[3] / ".auditai"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sub(name: str) -> Path:
    path = pr_data_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def repo_dir(pr_key: str) -> Path:
    """克隆持久化目录: 复用 AutoCVE get_project_persistent_source_path 的
    「稳定根 + 唯一键子目录」模式(zip_storage.py:49)。"""
    return _sub("repos") / pr_key


def diff_path(pr_key: str) -> Path:
    return _sub("diffs") / f"{pr_key}.diff"


def context_path(pr_key: str) -> Path:
    return _sub("context") / f"{pr_key}.json"


def review_path(review_id: str) -> Path:
    return _sub("reviews") / f"{review_id}.json"
