import subprocess
from pathlib import Path

import pytest

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.services.input_builder import build_review_snapshot
from app.domains.deep_review.tools.catalog import build_review_tools
from app.contracts.tools import ToolExecutionContext


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture()
def binary_rename_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    original = bytes(range(100))
    (repo / "before.txt").write_bytes(original)
    git(repo, "add", "before.txt")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "mv", "before.txt", "after.txt")
    changed = bytearray(original)
    changed[-1] ^= 1
    (repo / "after.txt").write_bytes(changed)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "binary rename")
    return repo


async def test_binary_rename_is_not_readable(binary_rename_repo: Path) -> None:
    base = git(binary_rename_repo, "rev-parse", "HEAD^")
    head = git(binary_rename_repo, "rev-parse", "HEAD")
    snapshot = build_review_snapshot(
        ReviewInput(repo_path=str(binary_rename_repo), base_ref=base, head_ref=head),
        DeepReviewConfig(),
    )
    change = next(item for item in snapshot.changes if item.path == "after.txt")
    assert change.change_type == "binary"
    tools = build_review_tools(
        repo_path=str(binary_rename_repo), head_commit=head, snapshot=snapshot, config=DeepReviewConfig()
    )
    file_read = tools[0]
    result = await file_read.execute(
        file_read.validate_input({"path": "after.txt"}),
        ToolExecutionContext(session_id="s", turn_id="t", tool_use_id="u", tool_call_id="c"),
    )
    assert result.output_payload["error"] == "binary_file"
