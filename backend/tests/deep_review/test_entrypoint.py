from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


@pytest.fixture()
def cli_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "old.py").write_text("old()\n", encoding="utf-8")
    (repo / ".env").write_text("secret = base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    (repo / "new.py").write_text("print('new')\n", encoding="utf-8")
    git(repo, "rm", "old.py")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "head")
    return repo


def run_cli(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "app.domains.deep_review", *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd or BACKEND_ROOT),
        env=env,
        timeout=30,
    )


def base_head(repo: Path) -> tuple[str, str]:
    head = git(repo, "rev-parse", "HEAD")
    return git(repo, "rev-parse", "HEAD^"), head


def test_cli_preparation_writes_output_and_keeps_stdout_empty(cli_repo: Path, tmp_path: Path) -> None:
    base, head = base_head(cli_repo)
    output = tmp_path / "preparation.json"
    store_dir = tmp_path / "events"
    result = run_cli(
        [
            "--repo", str(cli_repo),
            "--base", base,
            "--head", head,
            "--through", "anatomy",
            "--output", str(output),
            "--store-dir", str(store_dir),
        ]
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert "deep_review.stage_started" in result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["mode"] == "preparation"
    assert report["pipeline_complete"] is False
    assert report["completed_stage"] == "anatomy"
    assert (store_dir / report["run_id"] / "events.jsonl").is_file()


def test_cli_without_output_writes_json_to_stdout(cli_repo: Path, tmp_path: Path) -> None:
    base, head = base_head(cli_repo)
    result = run_cli(
        [
            "--repo", str(cli_repo),
            "--base", base,
            "--head", head,
            "--store-dir", str(tmp_path / "events"),
        ]
    )

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["pipeline_complete"] is False
    assert "deep_review.stage_started" in result.stderr


def test_cli_invalid_ref_returns_two_without_success_report(cli_repo: Path, tmp_path: Path) -> None:
    head = git(cli_repo, "rev-parse", "HEAD")
    output = tmp_path / "report.json"
    result = run_cli(
        [
            "--repo", str(cli_repo),
            "--base", "does-not-exist",
            "--head", head,
            "--output", str(output),
            "--store-dir", str(tmp_path / "events"),
        ]
    )

    assert result.returncode == 2
    assert output.exists() is False
    assert "ref not found" in result.stderr


def test_cli_unsupported_through_returns_two(cli_repo: Path, tmp_path: Path) -> None:
    base, head = base_head(cli_repo)
    result = run_cli(
        [
            "--repo", str(cli_repo),
            "--base", base,
            "--head", head,
            "--through", "reviewer",
            "--store-dir", str(tmp_path / "events"),
        ]
    )

    assert result.returncode == 2
    assert "invalid choice" in result.stderr
