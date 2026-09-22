import subprocess
from pathlib import Path

import pytest

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.services.blast_radius import (
    BlastRadiusError,
    build_import_graph,
    compute_blast_radius,
)


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


async def test_batch_reader_matches_unique_path_suffix_alias(tmp_path: Path) -> None:
    repo = tmp_path / "alias-repo"
    (repo / "backend" / "app").mkdir(parents=True)
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "backend" / "app" / "foo.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "backend" / "app" / "caller.py").write_text(
        "from app.foo import value\n", encoding="utf-8"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    head = git(repo, "rev-parse", "HEAD")
    config = DeepReviewConfig()
    diagnostics: list[str] = []

    graph = await build_import_graph(str(repo), head, config, diagnostics=diagnostics)

    assert graph["backend/app/caller.py"] == ["backend/app/foo.py"]
    assert await compute_blast_radius(
        ["backend/app/foo.py"], str(repo), head, config, diagnostics=diagnostics
    ) == ["backend/app/caller.py"]
    assert diagnostics == []


async def test_file_limit_degrades_blast_radius(tmp_path: Path) -> None:
    repo = tmp_path / "limit-repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("from b import value\n", encoding="utf-8")
    (repo / "b.py").write_text("value = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    head = git(repo, "rev-parse", "HEAD")

    with pytest.raises(BlastRadiusError, match="file limit exceeded"):
        await compute_blast_radius(
            ["b.py"], str(repo), head, DeepReviewConfig(max_import_graph_files=1)
        )


async def test_oversized_file_is_skipped_with_diagnostic(tmp_path: Path) -> None:
    repo = tmp_path / "oversized-repo"
    (repo / "pkg").mkdir(parents=True)
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "pkg" / "changed.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "large.py").write_text("# " + ("x" * 100) + "\n", encoding="utf-8")
    (repo / "caller.py").write_text("from pkg.changed import value\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    head = git(repo, "rev-parse", "HEAD")
    diagnostics: list[str] = []

    graph = await build_import_graph(
        str(repo),
        head,
        DeepReviewConfig(max_file_bytes=32),
        diagnostics=diagnostics,
    )

    assert "large.py" not in graph
    assert graph["caller.py"] == ["pkg/changed.py"]
    assert diagnostics == ["import_graph_oversized_files_skipped: 1"]
