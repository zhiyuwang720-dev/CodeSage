"""spec §6 test_agent_permission_matrix: 各视角只能调用自己矩阵内的工具。"""
import pytest

from app.domains.pr_review.orchestrator import TOOL_MATRICES
from app.contracts.review_execution import sha256_bytes
from app.domains.pr_review.diff_index import parse_unified_diff
from app.tool_gateway.builder import build_runtime_tool_catalog
from app.tool_gateway.pr_review import PrReviewToolContext, build_pr_review_tool_catalog
from app.tool_gateway.registry import build_runtime_tool_registry
from tests.pr_review.fake_runtime import build_review_runner, make_session_factory


@pytest.fixture()
def registry_names(tmp_path):
    factory = make_session_factory(tmp_path)
    _, _, registry = build_review_runner(factory, object().__new__(object), project_root=tmp_path, agent_type="review:security")
    return {t.name for t in registry.enabled_tools()}


def _names(tmp_path, agent_type: str, allowlist: set[str] | None = None):
    del allowlist
    factory = make_session_factory(tmp_path)
    _, _, registry = build_review_runner(
        factory, object().__new__(object), project_root=tmp_path, agent_type=agent_type,
    )
    return {t.name for t in registry.enabled_tools()}


def test_review_registry_has_finalize_review_not_finding(tmp_path):
    names = _names(tmp_path, "review:security")
    assert "FinalizeReview" in names
    assert "FinalizeVulnerabilityReports" not in names


def test_non_review_registry_has_no_terminal_finalizer(tmp_path):
    """非 review:* 注册表不挂任何终点工具(引擎 finalizer 仅绑定 review:* 视角)。"""
    factory = make_session_factory(tmp_path)
    session_store = factory()
    registry = build_runtime_tool_registry(
        session_store=session_store,
        file_tools=build_runtime_tool_catalog(project_root=str(tmp_path)),
        agent_type="generic",
    )
    names = {t.name for t in registry.enabled_tools()}
    assert "FinalizeReview" not in names
    assert not {"FinalizeReview", "FinalizeVulnerabilityReports"} & names


def test_allowlist_filters_tools(tmp_path):
    """Plan21 PR allowlist only retains bounded domain tools and finalizer."""
    factory = make_session_factory(tmp_path)
    session_store = factory()

    registry = build_runtime_tool_registry(
        session_store=session_store,
        file_tools=build_pr_review_tool_catalog(
            PrReviewToolContext(
                run_id="run",
                diff_index=parse_unified_diff(
                    "diff --git a/a.py b/a.py\n@@ -0,0 +1 @@\n+x=1\n",
                    diff_sha256=sha256_bytes(b"fixture"),
                ),
                mode="diff_only",
            )
        ),
        agent_type="review:architecture",
        tool_allowlist=TOOL_MATRICES["architecture"],
    )
    names = {t.name for t in registry.enabled_tools()}
    assert "FinalizeReview" in names, "终点工具始终保留"
    assert {"ListChanges", "ReadDiff", "SearchDiff"} <= names
    assert not {"Read", "Glob", "Grep", "Bash", "PowerShell", "Write", "Skill", "ToolSearch"} & names


def test_matrices_are_view_scoped():
    """All perspectives use the same bounded PR-domain capability set."""
    assert TOOL_MATRICES["architecture"] <= TOOL_MATRICES["security"]
    for perspective, allowlist in TOOL_MATRICES.items():
        assert {"ListChanges", "ReadDiff", "SearchDiff"} <= allowlist
        assert not {"Read", "Glob", "Grep", "Bash", "PowerShell", "Write", "Skill", "ToolSearch"} & allowlist, perspective
