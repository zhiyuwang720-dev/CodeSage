"""spec §6 test_agent_permission_matrix: 各视角只能调用自己矩阵内的工具。"""
import pytest

from app.services.pr_review.orchestrator import TOOL_MATRICES
from app.services.runtime_core.runtime_tool_registry import build_runtime_tool_registry
from tests.pr_review.fake_runtime import build_review_runner, make_session_factory


@pytest.fixture()
def registry_names(tmp_path):
    factory = make_session_factory(tmp_path)
    _, _, registry = build_review_runner(factory, object().__new__(object), project_root=tmp_path, agent_type="review:security")
    return {t.name for t in registry.enabled_tools()}


def _names(tmp_path, agent_type: str, allowlist: set[str] | None = None):
    factory = make_session_factory(tmp_path)
    _, _, registry = build_review_runner(
        factory, object().__new__(object), project_root=tmp_path, agent_type=agent_type,
    )
    return {t.name for t in registry.enabled_tools()}


def test_review_registry_has_finalize_review_not_finding(tmp_path):
    names = _names(tmp_path, "review:security")
    assert "FinalizeReview" in names
    assert "FinalizeFinding" not in names


def test_finding_registry_unaffected(tmp_path):
    """finding 注册表保持原行为(FinalizeFinding), 不含 review 终点。"""
    from app.services.agent.tools.shared_catalog import build_shared_agent_tool_catalog

    factory = make_session_factory(tmp_path)
    session_store = factory()
    registry = build_runtime_tool_registry(
        session_store=session_store,
        agent_tools=build_shared_agent_tool_catalog(project_root=str(tmp_path)),
        agent_type="finding",
        include_finding_finalizer=True,
    )
    names = {t.name for t in registry.enabled_tools()}
    assert "FinalizeFinding" in names
    assert "FinalizeReview" not in names


def test_allowlist_filters_tools(tmp_path):
    """tool_allowlist 裁剪: 架构视角(只读矩阵)不应有 Bash/Write。"""
    factory = make_session_factory(tmp_path)
    session_store = factory()
    from app.services.agent.tools.shared_catalog import build_shared_agent_tool_catalog

    registry = build_runtime_tool_registry(
        session_store=session_store,
        agent_tools=build_shared_agent_tool_catalog(project_root=str(tmp_path)),
        agent_type="review:architecture",
        include_finding_finalizer=False,
        tool_allowlist=TOOL_MATRICES["architecture"],
    )
    names = {t.name for t in registry.enabled_tools()}
    assert "FinalizeReview" in names, "终点工具始终保留"
    assert "Bash" not in names and "PowerShell" not in names, "架构视角无 Shell"
    assert "Read" in names or "Glob" in names, "只读工具保留"


def test_matrices_are_view_scoped():
    """架构最严格; Security 与 Quality 可用 Bash; 三视角互相不可见对方专属配置。"""
    assert TOOL_MATRICES["architecture"] <= TOOL_MATRICES["security"]
    assert "Bash" in TOOL_MATRICES["security"] and "Bash" in TOOL_MATRICES["quality"]
    for perspective, allowlist in TOOL_MATRICES.items():
        assert {"Read", "Glob", "Grep"} <= allowlist, f"{perspective} 必备只读三件套"
