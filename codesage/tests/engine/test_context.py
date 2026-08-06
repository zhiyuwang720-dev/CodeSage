"""Context bundle tests: AGENTS.md discovery/budget/override, git snapshot (S3)."""

import shutil
import subprocess

import pytest

from codesage.engine.context import MAX_AGENTS_CHARS, build_context_bundle


def test_no_agents_md_only_date(tmp_path):
    bundle = build_context_bundle(tmp_path)
    assert [t for t, _ in bundle.sections] == ["currentDate"]  # tmp_path is no git repo
    assert bundle.get("currentDate") == "Today's date is " + __import__("datetime").date.today().isoformat() + "."


def test_single_level_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("root rules")
    bundle = build_context_bundle(tmp_path)
    assert [t for t, _ in bundle.sections] == ["currentDate", "agentsMd"]
    assert bundle.get("agentsMd") == "root rules"


def test_nested_agents_near_file_last(tmp_path):
    """Far file lands before the near file (recency: near wins attention)."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "AGENTS.md").write_text("root rules")
    (sub / "AGENTS.md").write_text("sub rules")
    bundle = build_context_bundle(sub)
    agents = [text for t, text in bundle.sections if t == "agentsMd"]
    assert agents == ["root rules", "sub rules"]


def test_32kb_budget_near_survives_far_truncated(tmp_path):
    """Near file stays complete; the far overflow is truncated to the remainder."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "AGENTS.md").write_text("x" * (MAX_AGENTS_CHARS + 500))  # overflows alone
    (sub / "AGENTS.md").write_text("near content")  # 12 chars
    bundle = build_context_bundle(sub)
    agents = [text for t, text in bundle.sections if t == "agentsMd"]
    assert agents == ["x" * (MAX_AGENTS_CHARS - 12), "near content"]


def test_budget_drops_farthest_when_truncation_exhausted(tmp_path):
    """A middle file truncated to zero leaves the far file dropped entirely."""
    sub = tmp_path / "sub"
    deep = sub / "deep"
    deep.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("far")  # smallest, but farthest — dropped
    (sub / "AGENTS.md").write_text("x" * (MAX_AGENTS_CHARS + 100))  # eats the budget
    (deep / "AGENTS.md").write_text("near")  # stays complete (4 chars)
    bundle = build_context_bundle(deep)
    agents = [text for t, text in bundle.sections if t == "agentsMd"]
    assert agents == ["x" * (MAX_AGENTS_CHARS - 4), "near"]


def test_override_replaces_discovery(tmp_path):
    override = tmp_path / "my-rules.md"
    override.write_text("override rules")
    (tmp_path / "AGENTS.md").write_text("discovered rules")
    bundle = build_context_bundle(tmp_path, override_file=override)
    agents = [text for t, text in bundle.sections if t == "agentsMd"]
    assert agents == ["override rules"]


def test_override_missing_yields_no_agents(tmp_path):
    """An explicitly named override that doesn't exist means NO auto discovery
    (fail-explicit: the user asked for that file, not for discovery)."""
    (tmp_path / "AGENTS.md").write_text("discovered rules")
    bundle = build_context_bundle(tmp_path, override_file=tmp_path / "nope.md")
    assert [t for t, _ in bundle.sections] == ["currentDate"]


def test_override_truncated_to_budget(tmp_path):
    override = tmp_path / "big.md"
    override.write_text("z" * (MAX_AGENTS_CHARS + 100))
    bundle = build_context_bundle(tmp_path, override_file=override)
    assert len(bundle.get("agentsMd")) == MAX_AGENTS_CHARS


# ---- git snapshot ----

def _init_repo(path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True, capture_output=True)


def test_git_snapshot_branch_and_commits(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True, capture_output=True)
    bundle = build_context_bundle(repo)
    snapshot = bundle.get("gitStatus")
    assert snapshot is not None
    assert "snapshot in time" in snapshot
    assert "Current branch: main" in snapshot
    assert "Recent commits:\n" in snapshot and "init" in snapshot
    assert "Status:" in snapshot


def test_git_snapshot_status_capped(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    for i in range(150):  # ~20 bytes/line → well over the 2000-char cap
        (repo / f"untracked-{i}.txt").write_text("u")
    bundle = build_context_bundle(repo)
    snapshot = bundle.get("gitStatus")
    status_block = snapshot.split("Status:")[1]
    assert "truncated beyond" in status_block


def test_git_snapshot_none_outside_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    bundle = build_context_bundle(tmp_path)
    assert bundle.get("gitStatus") is None


def test_git_snapshot_runs_commands_in_parallel(tmp_path, monkeypatch):
    """Serial git runs would take ~0.6s with 4×0.15s sleeps; parallel ~0.3s."""
    import time

    import codesage.engine.context as ctx_mod

    def slow_run(cwd, args):
        time.sleep(0.15)
        return "true" if "rev-parse" in args else "x"

    monkeypatch.setattr(ctx_mod, "_git_run", slow_run)
    start = time.monotonic()
    ctx_mod._git_snapshot(tmp_path)
    elapsed = time.monotonic() - start
    assert elapsed < 0.45, f"git commands ran serially: {elapsed:.2f}s"
