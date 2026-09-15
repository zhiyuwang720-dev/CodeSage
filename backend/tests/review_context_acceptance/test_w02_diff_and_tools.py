from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.contracts.models import ToolCallRequest, ToolExecutionPayload
from app.nodes.pr_review.contracts.review_execution import sha256_bytes
from app.contracts.tools import RuntimeTool, ToolExecutionContext
from app.nodes.pr_review.domain.diff_index import parse_unified_diff
from app.infrastructure.repositories.snapshots import GitSnapshotReader, create_snapshot_ref
from app.nodes.pr_review.tools.pr_review import PrReviewToolContext, build_pr_review_tool_catalog
from app.node_runtime.tool_gateway.runtime import ToolGateway, ToolRegistry


DIFF = """diff --git a/src/old.py b/src/new.py
similarity index 80%
rename from src/old.py
rename to src/new.py
--- a/src/old.py
+++ b/src/new.py
@@ -1,2 +1,3 @@
 value = 1
+danger = value
 return value
diff --git a/assets/blob.bin b/assets/blob.bin
new file mode 100644
Binary files /dev/null and b/assets/blob.bin differ
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-gone = True
"""


def _execution_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id="session", turn_id="turn", tool_use_id="use", tool_call_id="call"
    )


def test_diff_index_registers_rename_binary_delete_and_stable_units():
    digest = sha256_bytes(DIFF.encode())
    first = parse_unified_diff(DIFF, diff_sha256=digest)
    second = parse_unified_diff(DIFF, diff_sha256=digest)
    assert [file.status for file in first.files] == ["renamed", "binary", "deleted"]
    assert len(first.change_units) == 3
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.files[0].hunks[0].lines[1].new_line == 2


@pytest.mark.asyncio
async def test_diff_only_catalog_is_bounded_and_has_stable_cursor():
    index = parse_unified_diff(DIFF, diff_sha256=sha256_bytes(DIFF.encode()))
    context = PrReviewToolContext(run_id="run", diff_index=index, mode="diff_only")
    catalog = build_pr_review_tool_catalog(context)
    assert {tool.name for tool in catalog} == {"ListChanges", "ReadDiff", "SearchDiff"}
    list_tool = next(tool for tool in catalog if tool.name == "ListChanges")
    first = await list_tool.execute(list_tool.validate_input({"page_size": 1}), _execution_context())
    assert first.is_error is False
    payload = first.output_payload
    assert payload["status"] == "partial"
    assert payload["next_cursor"]
    assert len(first.content.encode()) <= 16 * 1024
    second = await list_tool.execute(
        list_tool.validate_input({"page_size": 1, "cursor": payload["next_cursor"]}),
        _execution_context(),
    )
    assert second.output_payload["items"][0]["status"] == "binary"
    bad = await list_tool.execute(
        list_tool.validate_input({"page_size": 2, "cursor": payload["next_cursor"]}),
        _execution_context(),
    )
    assert bad.is_error and bad.output_payload["error_code"] == "cursor_invalid"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.mark.asyncio
async def test_source_tools_search_unmodified_caller_at_fixed_head(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "plan21@example.invalid")
    _git(repo, "config", "user.name", "Plan 21")
    (repo / "service.py").write_text("def changed():\n    return 1\n", encoding="utf-8")
    (repo / "caller.py").write_text("from service import changed\nresult = changed()\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "service.py").write_text("def changed():\n    return 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "head")
    head = _git(repo, "rev-parse", "HEAD")
    diff = subprocess.check_output(["git", "-C", str(repo), "diff", base, head]).decode()
    snapshot = await create_snapshot_ref(repo, repository_key="repo", base_revision=base, head_revision=head, diff_basis="two_dot")
    context = PrReviewToolContext(
        run_id="run",
        diff_index=parse_unified_diff(diff, diff_sha256=sha256_bytes(diff.encode())),
        mode="repository_required",
        snapshot_reader=GitSnapshotReader(repo, snapshot),
    )
    catalog = {tool.name: tool for tool in build_pr_review_tool_catalog(context)}
    assert {"ReadSource", "SearchSource"} <= set(catalog)
    search = catalog["SearchSource"]
    result = await search.execute(
        search.validate_input({"side": "head", "query": "changed()", "path_prefix": "caller.py"}),
        _execution_context(),
    )
    assert result.output_payload["status"] == "ok"
    assert result.output_payload["items"][0]["path"] == "caller.py"


class _ReturnedErrorTool(RuntimeTool):
    name = "ReturnedError"

    async def execute(self, parsed_input, context):
        return ToolExecutionPayload(
            content="source vanished",
            output_payload={"error_code": "source_unavailable"},
            metadata={"error_kind": "source_unavailable"},
            is_error=True,
        )


class _Store:
    def __init__(self):
        self.completed = None

    def start_tool_call(self, **kwargs):
        return "stored-call"

    def complete_tool_call(self, tool_call_id, **kwargs):
        self.completed = {"tool_call_id": tool_call_id, **kwargs}

    def load_runtime_state(self, session_id):
        return SimpleNamespace(metadata={}, permission_mode="default", agent_states={})

    def create_checkpoint(self, **kwargs):
        return None


@pytest.mark.asyncio
async def test_returned_is_error_is_persisted_as_failed_with_content():
    store = _Store()
    gateway = ToolGateway(session_store=store, tool_registry=ToolRegistry([_ReturnedErrorTool()]))
    records = await gateway.execute_tool_calls(
        session_id="session",
        turn_id="turn",
        tool_calls=[ToolCallRequest(id="use", name="ReturnedError", input={})],
    )
    assert records[0].status == "failed"
    assert records[0].result.is_error is True
    assert records[0].result.content == "source vanished"
    assert store.completed["status"] == "failed"
    assert store.completed["output_payload"]["error_code"] == "source_unavailable"
