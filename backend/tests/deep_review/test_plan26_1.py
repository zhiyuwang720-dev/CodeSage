import json
import subprocess
from pathlib import Path

import pytest

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.services.blast_radius import analyze_blast_radius
from app.domains.deep_review.services.head_tree import validate_head_context_paths


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def review_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "caller.py").write_text("from pkg.changed import VALUE\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (repo / "notes.txt").write_text("context\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "head")
    return repo, git(repo, "rev-parse", "HEAD")


async def test_python_blast_streams_direct_head_import_without_legacy_graph(
    review_repo: tuple[Path, str],
) -> None:
    repo, head = review_repo
    result, index = await analyze_blast_radius(
        ["pkg/changed.py"], str(repo), head, DeepReviewConfig(),
    )
    assert index is not None
    assert result.coverage_by_language == {"python": "analyzed"}
    assert result.displayed_paths == ["caller.py"]
    assert result.observed_hint_count == 1
    assert result.scanned_file_count >= 2
    assert result.scanned_bytes > 0
    assert not result.truncated
    assert len(json.dumps(result.prompt_projection(), ensure_ascii=False, sort_keys=True).encode()) <= 1500


async def test_unsupported_language_never_scans_python_tree() -> None:
    result, index = await analyze_blast_radius(
        ["main.rs", "web/image.xyz"], "unused", "unused", DeepReviewConfig(),
    )
    assert index is None
    assert result.coverage_by_language == {"rust": "unsupported", "unsupported:.xyz": "unsupported"}
    assert result.displayed_paths == []


async def test_python_blast_uses_fixed_head_not_worktree(review_repo: tuple[Path, str]) -> None:
    repo, head = review_repo
    (repo / "caller.py").write_text("# uncommitted removal of import\n", encoding="utf-8")
    result, _ = await analyze_blast_radius(
        ["pkg/changed.py"], str(repo), head, DeepReviewConfig(),
    )
    assert result.displayed_paths == ["caller.py"]
    assert ".env" not in json.dumps(result.prompt_projection())


async def test_blast_source_limit_is_separate_from_file_read_limit(
    review_repo: tuple[Path, str],
) -> None:
    repo, _head = review_repo
    (repo / "caller.py").write_text(
        "from pkg.changed import VALUE\n" + ("# padding\n" * 120_000),
        encoding="utf-8",
    )
    git(repo, "add", "caller.py")
    git(repo, "commit", "-m", "large caller")
    head = git(repo, "rev-parse", "HEAD")
    assert (repo / "caller.py").stat().st_size > DeepReviewConfig().max_file_bytes
    result, _ = await analyze_blast_radius(
        ["pkg/changed.py"], str(repo), head, DeepReviewConfig(),
    )
    assert result.displayed_paths == ["caller.py"]
    assert result.coverage_by_language == {"python": "analyzed"}


async def test_tree_cap_degrades_blast_but_planner_can_validate_exact_context(
    review_repo: tuple[Path, str],
) -> None:
    repo, head = review_repo
    config = DeepReviewConfig(max_import_tree_bytes=1)
    result, index = await analyze_blast_radius(["pkg/changed.py"], str(repo), head, config)
    assert result.coverage_by_language == {"python": "degraded"}
    assert result.truncated
    assert index is None
    legal = await validate_head_context_paths(
        str(repo), head, ["caller.py", "notes.txt", ".env", "missing.py"], config,
        head_tree=index,
    )
    assert legal == {"caller.py", "notes.txt"}


async def test_bounded_sink_keeps_prompt_small_and_stable(tmp_path: Path) -> None:
    repo = tmp_path / "many"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")
    for index in range(80):
        (repo / f"caller_{index:03}.py").write_text(
            "from changed import VALUE\n", encoding="utf-8",
        )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    first, _ = await analyze_blast_radius(["changed.py"], str(repo), head, DeepReviewConfig())
    second, _ = await analyze_blast_radius(["changed.py"], str(repo), head, DeepReviewConfig())
    assert first.observed_hint_count == 80
    assert len(first.hints) <= 48
    assert len(first.displayed_paths) <= 12
    assert first.truncated
    assert first.prompt_projection() == second.prompt_projection()
    assert len(json.dumps(first.prompt_projection(), ensure_ascii=False, sort_keys=True).encode()) <= 1500
