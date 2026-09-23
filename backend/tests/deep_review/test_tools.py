import json
import subprocess
from pathlib import Path

import pytest

from app.contracts.models import ToolExecutionPayload
from app.contracts.tools import ToolExecutionContext
from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.services.input_builder import build_review_snapshot
from app.domains.deep_review.tools.catalog import build_review_tools


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, encoding="utf-8")
    return result.stdout.strip()


@pytest.fixture()
def tools(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "deleted.py").write_text("old()\n", encoding="utf-8")
    (repo / "context_helper.py").write_text("helper_marker = 1\n", encoding="utf-8")
    (repo / "image.png").write_bytes(b"\x00PNG_binary")
    git(repo, "add", "a.py")
    git(repo, "add", "deleted.py")
    git(repo, "add", "context_helper.py")
    git(repo, "add", "image.png")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    git(repo, "add", "a.py")
    git(repo, "commit", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "rm", "deleted.py")
    git(repo, "commit", "-m", "delete")
    head = git(repo, "rev-parse", "HEAD")
    snapshot = build_review_snapshot(
        ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head), DeepReviewConfig(max_tool_read_lines=1)
    )
    return build_review_tools(repo_path=str(repo), head_commit=snapshot.head_commit, snapshot=snapshot, config=DeepReviewConfig(max_tool_read_lines=1)), snapshot


async def execute(tool, raw: dict) -> ToolExecutionPayload:
    return await tool.execute(tool.validate_input(raw), ToolExecutionContext(
        session_id="s", turn_id="t", tool_use_id="u", tool_call_id="c"
    ))


async def test_file_read_and_diff(tools) -> None:
    file_read, file_diff, _find, _search = tools[0]
    snapshot = tools[1]
    read = await execute(file_read, {"path": "a.py", "start_line": 1, "end_line": 1})
    assert read.is_error is False
    assert "1|value = 2" in read.content
    diff = await execute(file_diff, {"path": "a.py"})
    assert json.loads(diff.content)["path"] == "a.py"
    deleted = await execute(file_read, {"path": "deleted.py"})
    assert deleted.output_payload["error"] == "unavailable_deleted_file"
    assert "deleted.py" in snapshot.allowed_paths


async def test_find_and_search(tools) -> None:
    _read, _diff, find, search = tools[0]
    found = await execute(find, {"query": "a.py"})
    assert found.output_payload["paths"] == ["a.py"]
    searched = await execute(search, {"query": "value"})
    assert searched.output_payload["matches"][0]["path"] == "a.py"


async def test_search_can_reach_nonsecret_unmodified_context(tools) -> None:
    _read, _diff, _find, search = tools[0]
    repo = Path(tools[1].input.repo_path)
    context_path = repo / "context_helper.py"
    context_path.write_text("helper_marker = 1\n", encoding="utf-8")
    result = await execute(search, {"query": "helper_marker"})
    assert result.output_payload["matches"][0]["path"] == "context_helper.py"
    context_path.unlink()


async def test_file_read_rejects_unmodified_binary_context(tools) -> None:
    file_read, _diff, _find, _search = tools[0]
    result = await execute(file_read, {"path": "image.png"})
    assert result.output_payload["error"] == "binary_file"


async def test_file_read_diff_cannot_read_excluded_change(tmp_path: Path) -> None:
    repo = tmp_path / "filtered-repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "excluded.py").write_text("value = 1\n", encoding="utf-8")
    git(repo, "add", "excluded.py")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "excluded.py").write_text("value = 2\n", encoding="utf-8")
    git(repo, "add", "excluded.py")
    git(repo, "commit", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    snapshot = build_review_snapshot(
        ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head),
        DeepReviewConfig(exclude_paths=["excluded.py"]),
    )
    tools = build_review_tools(repo_path=str(repo), head_commit=head, snapshot=snapshot, config=DeepReviewConfig())
    file_diff = tools[1]
    rejected = await execute(file_diff, {"path": "excluded.py"})
    assert rejected.is_error
    assert rejected.output_payload["error"] == "invalid_path"
    assert "filtered review context" in rejected.output_payload["message"]


async def test_file_read_is_fixed_to_head(tools) -> None:
    file_read, _diff, _find, _search = tools[0]
    repo = Path(tools[1].input.repo_path)
    target = repo / "a.py"
    original = target.read_text(encoding="utf-8")
    target.write_text("mutated = true\n", encoding="utf-8")
    result = await execute(file_read, {"path": "a.py"})
    assert "value = 2" in result.content
    assert "mutated" not in result.content
    target.write_text(original, encoding="utf-8")


async def test_tools_reject_outside_and_deleted_source(tools) -> None:
    file_read, file_diff, _find, search = tools[0]
    with pytest.raises(Exception, match="invalid repository path"):
        file_read.validate_input({"path": "../outside.py"})
    invalid_diff = await execute(file_diff, {"path": "../outside.py"})
    assert invalid_diff.is_error
    assert invalid_diff.output_payload["error"] == "invalid_path"
    deleted = await execute(file_read, {"path": "deleted.py"})
    assert deleted.output_payload["error"] == "unavailable_deleted_file"
    with pytest.raises(Exception, match="invalid repository path"):
        search.validate_input({"query": "value", "path_prefix": "../outside"})
