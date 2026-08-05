"""V1 acceptance: real DeepSeek API end-to-end.

The V1 acceptance checklist (docs/specs/07-cli.md):
1. single-shot yolo: a file-creation task completes, artifact exists
2. deny rule: the task does NOT execute, model sees the denial
3. audit trail: events recorded
Skips without DEEPSEEK_API_KEY (reads backend/.env as fallback).
"""

import io
import json
import os
import re
from pathlib import Path

import pytest

from codesage.cli.assemble import build_loop
from codesage.cli.repl import run_single_turn
from codesage.config import paths

_ACCEPTANCE_FILE = "hello.md"
_ACCEPTANCE_CONTENT = "V1 验收通过"


def _deepseek_key() -> str:
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if key:
        return key
    env_path = Path(__file__).resolve().parents[3] / "backend" / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*DEEPSEEK_API_KEY\s*=\s*(.+)", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return ""


def _key_or_skip() -> str:
    key = _deepseek_key()
    if not key:
        pytest.skip("DEEPSEEK_API_KEY not set — skipping V1 acceptance")
    # point the CLI's default (main) profile at DeepSeek via env
    os.environ["CODESAGE_MODEL"] = os.getenv("CODESAGE_MODEL", "deepseek-v4-flash")
    os.environ["CODESAGE_BASE_URL"] = os.getenv("CODESAGE_BASE_URL", "https://api.deepseek.com/v1")
    os.environ["CODESAGE_API_KEY_ENV"] = os.getenv("CODESAGE_API_KEY_ENV", "DEEPSEEK_API_KEY")
    return key


def _audit_events(root: Path) -> list[dict]:
    path = root / ".codesage" / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_v1_acceptance_create_file(tmp_path, monkeypatch):
    os.environ["DEEPSEEK_API_KEY"] = _key_or_skip()
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / ".codesage")

    loop = build_loop(cwd=tmp_path, mode="yolo")
    buf = io.StringIO()
    await run_single_turn(
        loop,
        f"请创建 {_ACCEPTANCE_FILE},内容为:{_ACCEPTANCE_CONTENT}。不要使用 shell,直接写文件。",
        out=buf,
    )
    out = buf.getvalue()

    target = tmp_path / _ACCEPTANCE_FILE
    assert target.exists(), f"file not created — output:\n{out}"
    assert _ACCEPTANCE_CONTENT in target.read_text(encoding="utf-8", errors="replace")

    events = _audit_events(tmp_path)
    assert events, "audit trail is empty"
    assert any(e["tool_name"] == "Write" and e["decision"] == "allow" for e in events), events


@pytest.mark.asyncio
async def test_v1_acceptance_deny_blocks(tmp_path, monkeypatch):
    os.environ["DEEPSEEK_API_KEY"] = _key_or_skip()
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / ".codesage")
    local = tmp_path / ".codesage"
    local.mkdir(exist_ok=True)
    (local / "settings.local.json").write_text(
        json.dumps({"permissions": {"deny": ["Write"]}}), encoding="utf-8"
    )

    loop = build_loop(cwd=tmp_path, mode="yolo")  # yolo must NOT bypass deny
    buf = io.StringIO()
    await run_single_turn(
        loop,
        f"请创建 {_ACCEPTANCE_FILE},内容为:x。不要使用 shell。",
        out=buf,
    )

    assert not (tmp_path / _ACCEPTANCE_FILE).exists(), "deny rule was bypassed"
    events = _audit_events(tmp_path)
    assert any(e["decision"] == "deny" and e["tool_name"] == "Write" for e in events), events
