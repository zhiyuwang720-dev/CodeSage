import subprocess
from pathlib import Path

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.services.blast_radius import build_import_graph, compute_blast_radius


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


async def test_import_graph_and_blast_radius(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "caller.py").write_text("from pkg.changed import VALUE\n", encoding="utf-8")
    git(repo, "add", "pkg", "caller.py")
    git(repo, "commit", "-m", "base")
    head = git(repo, "rev-parse", "HEAD")
    config = DeepReviewConfig()

    graph = await build_import_graph(str(repo), head, config)
    assert graph["caller.py"] == ["pkg/changed.py"]
    assert await compute_blast_radius(["pkg/changed.py"], str(repo), head, config, import_graph=graph) == ["caller.py"]


async def test_relative_and_from_module_imports(tmp_path: Path) -> None:
    repo = tmp_path / "relative-repo"
    (repo / "pkg").mkdir(parents=True)
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "pkg" / "relative_caller.py").write_text("from .changed import VALUE\n", encoding="utf-8")
    (repo / "from_caller.py").write_text("from pkg import changed\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    head = git(repo, "rev-parse", "HEAD")

    graph = await build_import_graph(str(repo), head, DeepReviewConfig())
    assert graph["pkg/relative_caller.py"] == ["pkg/changed.py"]
    assert "pkg/changed.py" in graph["from_caller.py"]
