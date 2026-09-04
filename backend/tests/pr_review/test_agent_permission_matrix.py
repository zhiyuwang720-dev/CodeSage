"""spec §6 test_agent_permission_matrix: 各视角只能调用自己矩阵内的工具。"""
import pytest

from app.services.pr_review.orchestrator import TOOL_MATRICES
from app.services.tooling.builder import build_runtime_tool_catalog
from app.services.tooling.registry import build_runtime_tool_registry
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
    """tool_allowlist 裁剪: 架构视角按矩阵保留 Read/Glob/Grep/Bash/PowerShell/Skill, 但无 Write。"""
    factory = make_session_factory(tmp_path)
    session_store = factory()

    registry = build_runtime_tool_registry(
        session_store=session_store,
        file_tools=build_runtime_tool_catalog(project_root=str(tmp_path)),
        agent_type="review:architecture",
        tool_allowlist=TOOL_MATRICES["architecture"],
    )
    names = {t.name for t in registry.enabled_tools()}
    assert "FinalizeReview" in names, "终点工具始终保留"
    assert "Bash" in names, "架构视角含 Bash(重读跨文件引用、验证构建/依赖)"
    assert "PowerShell" in names, "Windows 上 PowerShell 是 Bash(WSL 启动器)的可靠兜底 shell, 已放行"
    assert "Write" not in names, "架构视角无 Write"
    assert "Read" in names or "Glob" in names, "只读工具保留"


def test_matrices_are_view_scoped():
    """Security/Quality/Architecture 均含只读三件套 + Bash(架构用于构建/依赖验证), 各自矩阵完整定义。"""
    assert TOOL_MATRICES["architecture"] <= TOOL_MATRICES["security"]
    assert "Bash" in TOOL_MATRICES["security"] and "Bash" in TOOL_MATRICES["quality"]
    for perspective, allowlist in TOOL_MATRICES.items():
        assert {"Read", "Glob", "Grep"} <= allowlist, f"{perspective} 必备只读三件套"
