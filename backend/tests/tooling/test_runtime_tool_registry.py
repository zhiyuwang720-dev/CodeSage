from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services.session.store import AuditSessionStore
from app.services.tooling.read import GlobRuntimeTool, GrepRuntimeTool, ReadRuntimeTool
from app.services.tooling.registry import build_runtime_tool_registry
from app.services.tooling.runtime import ToolExecutionContext


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def build_tool_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id="session-1",
        turn_id="turn-1",
        tool_use_id="tool-use-1",
        tool_call_id="tool-call-1",
    )


def build_file_tools(*, project_root: str = "D:/repo") -> list:
    return [
        ReadRuntimeTool(project_root=project_root),
        GlobRuntimeTool(project_root=project_root),
        GrepRuntimeTool(project_root=project_root),
    ]


def test_runtime_tool_registry_builder_exposes_shared_runtime_tools_for_agent():
    store = build_store()
    registry = build_runtime_tool_registry(
        session_store=store,
        file_tools=build_file_tools(),
        agent_type="recon",
        user_id="user-1",
    )

    tool_names = [item["name"] for item in registry.describe_tools()]
    skill_tool = registry.get("Skill")

    assert "Read" in tool_names
    assert "Glob" in tool_names
    assert "Grep" in tool_names
    assert "Write" in tool_names
    assert "Skill" in tool_names
    assert "TodoWrite" in tool_names
    assert "AskUser" in tool_names
    assert "EnterPlanMode" in tool_names
    assert "ExitPlanMode" in tool_names
    assert skill_tool is not None
    assert getattr(skill_tool, "_agent_type") == "recon"


def test_runtime_tool_descriptions_explain_audit_usage_and_continue_contract():
    store = build_store()
    registry = build_runtime_tool_registry(
        session_store=store,
        file_tools=build_file_tools(),
        agent_type="review:security",
        user_id="user-1",
    )

    descriptions = {item["name"]: item["description"] for item in registry.describe_tools()}

    assert "读取项目本地文件内容" in descriptions["Read"]
    assert "file_path 为相对项目根目录的文件路径" in descriptions["Read"]
    assert "Read 只能读取文件，不能枚举目录" in descriptions["Read"]
    assert "必须实际调用 Read、Grep、Glob" in descriptions["Read"]

    assert "在项目代码和配置文本中搜索关键字或正则表达式" in descriptions["Grep"]
    assert "pattern 是要搜索的关键字或正则表达式" in descriptions["Grep"]
    assert "搜索任务优先使用 Grep" in descriptions["Grep"]
    assert "不要只回复“我将继续搜索" in descriptions["Grep"]

    assert "按文件名或路径模式枚举项目文件" in descriptions["Glob"]
    assert "pattern 是 glob 模式" in descriptions["Glob"]
    assert "需要按内容查找时使用 Grep" in descriptions["Glob"]
    assert "不要只说明“接下来查找相关文件”" in descriptions["Glob"]
