from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.services.agent.tools.ask_user_runtime_tool import AskUserRuntimeTool
from app.services.agent.tools.base import AgentTool
from app.services.agent.tools.plan_mode_runtime_tool import EnterPlanModeRuntimeTool, ExitPlanModeRuntimeTool
from app.services.agent.tools.todo_runtime_tool import TodoWriteRuntimeTool
from app.services.review_runtime.models import ToolExecutionPayload
from app.services.review_runtime.skills import RuntimeSkillTool
from app.services.review_runtime.tools.finalize_finding import FinalizeFindingTool
from app.services.review_runtime.tools.finalize_vulnerability_reports import FinalizeVulnerabilityReportsTool
from app.services.triage_runtime.tools import (
    FinalizeTriageBatchTool,
    FinalizeTriageTool,
    GetScanFindingTool,
    GetTriageBatchTool,
)
from app.services.runtime_core.permission_runtime import ToolPermissionDecision
from app.services.runtime_core.runtime_guardrails import (
    APPROVAL_SCOPE_SINGLE_USE,
    consume_write_approval,
    has_write_approval,
    is_guardrails_enabled,
    register_write_approval,
)
from app.services.runtime_core.shell_runtime_tools import (
    BashRuntimeTool,
    PowerShellRuntimeTool,
    detect_bash_executable,
    detect_powershell_executable,
    is_powershell_runtime_tool_enabled,
)
from app.services.runtime_core.tool_runtime import (
    RUNTIME_SEARCH_TOOL_TIMEOUT_SECONDS,
    RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS,
    RuntimeTool,
    ToolExecutionContext,
    ToolRegistry,
)
from app.services.runtime_core.tool_search_runtime import ToolSearchRuntimeTool


GLOB_DEFAULT_MAX_RESULTS = 100
GLOB_HARD_MAX_RESULTS = 100
GREP_DEFAULT_MAX_RESULTS = 250
GREP_HARD_MAX_RESULTS = 250
TRUNCATED_RESULT_HINT = "结果被截断，使用更具体的 path 或 pattern。"


class ReadToolInput(BaseModel):
    file_path: str | None = Field(default=None, description="Path to a file relative to the project root.")
    file_paths: list[str] = Field(default_factory=list, description="Optional batch of related files to read together.")
    start_line: int | None = Field(default=None, description="Optional 1-based start line.")
    end_line: int | None = Field(default=None, description="Optional inclusive end line.")
    max_lines: int = Field(default=400, description="Maximum lines to return per file.")
    max_files: int = Field(default=6, description="Maximum files when batch reading.")


class GlobToolInput(BaseModel):
    path: str = Field(default=".", description="Directory relative to the project root.")
    pattern: str | None = Field(default=None, description="Optional glob pattern, for example **/*.java or *.xml.")
    recursive: bool = Field(default=True, description="Whether to walk child directories.")
    max_results: int = Field(default=GLOB_DEFAULT_MAX_RESULTS, description="Maximum files to return.")
    timeout_seconds: int = Field(default=RUNTIME_SEARCH_TOOL_TIMEOUT_SECONDS, ge=1, le=RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS)


class GrepToolInput(BaseModel):
    pattern: str = Field(description="Keyword or regular expression to search for.")
    path: str | None = Field(default=None, description="Optional directory relative to the project root.")
    glob: str | None = Field(default=None, description="Optional glob such as *.py or **/*.java.")
    case_sensitive: bool = Field(default=False, description="Whether the search is case sensitive.")
    max_results: int = Field(default=GREP_DEFAULT_MAX_RESULTS, description="Maximum number of matches to return.")
    is_regex: bool = Field(default=False, description="Whether pattern should be treated as regex.")
    timeout_seconds: int = Field(default=RUNTIME_SEARCH_TOOL_TIMEOUT_SECONDS, ge=1, le=RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS)


class WriteToolInput(BaseModel):
    path: str = Field(description="Required target path relative to the project root. Managed outputs should go under .auditai/.")
    content: str = Field(description="Required text content to write.")
    overwrite: bool = Field(default=False, description="Whether to overwrite an existing file.")


def _result_to_payload(result: Any) -> ToolExecutionPayload:
    output_payload = result.to_dict()
    return ToolExecutionPayload(
        content=result.to_string(),
        output_payload=output_payload,
        metadata={"success": result.success, **(result.metadata or {})},
        is_error=not result.success,
    )


def _append_truncation_hint(payload: ToolExecutionPayload, *, requested: int, limit: int) -> ToolExecutionPayload:
    if requested <= limit:
        return payload
    content = payload.content.rstrip()
    if TRUNCATED_RESULT_HINT not in content:
        content = f"{content}\n\n{TRUNCATED_RESULT_HINT}" if content else TRUNCATED_RESULT_HINT
    metadata = dict(payload.metadata or {})
    metadata.update(
        {
            "truncated": True,
            "requested_limit": requested,
            "applied_limit": limit,
        }
    )
    output_payload = dict(payload.output_payload or {})
    output_payload.update(
        {
            "truncated": True,
            "requested_limit": requested,
            "applied_limit": limit,
            "truncation_hint": TRUNCATED_RESULT_HINT,
        }
    )
    return ToolExecutionPayload(
        content=content,
        output_payload=output_payload,
        metadata=metadata,
        is_error=payload.is_error,
        context_modifier=payload.context_modifier,
    )


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        resolved = default
    return max(1, resolved)


def _infer_project_root(agent_tools: dict[str, AgentTool]) -> str | None:
    for key in ("read_file", "list_files", "search_code"):
        tool = agent_tools.get(key)
        project_root = getattr(tool, "project_root", None)
        if isinstance(project_root, str) and project_root.strip():
            return project_root
    return None


class CanonicalReadTool(RuntimeTool):
    name = "Read"
    description = (
        "读取项目本地文件内容。适合查看源代码、路由、控制器、服务、模型、配置、SQL、XML、模板、"
        "测试文件、依赖文件以及 Skill 引用文档。\n\n"
        "用法：\n"
        "- file_path 为相对项目根目录的文件路径。\n"
        "- 也可以使用 file_paths 一次读取少量强相关文件，例如同一个调用链上的 route/controller/service/model。\n"
        "- start_line 和 end_line 用于只读取已知相关片段；不确定位置时先读取完整文件或较大范围。\n"
        "- max_lines 控制单个文件最多返回的行数，长文件建议分段读取。\n"
        "- Read 只能读取文件，不能枚举目录；需要发现文件时先用 Glob，需要按关键字查找时用 Grep。\n\n"
        "审计要求：\n"
        "- 当你需要查看代码、确认实现、补齐 source/sink、验证调用链或提取代码片段时，使用 Read。\n"
        "- 如果你说“继续查看/继续追踪/让我检查/需要读取/确认实现”，必须实际调用 Read、Grep、Glob "
        "或其它合适工具，而不是只描述下一步计划。"
    )
    input_model = ReadToolInput

    def __init__(self, *, read_tool: AgentTool | None, read_many_tool: AgentTool | None = None):
        self._read_tool = read_tool
        self._read_many_tool = read_many_tool

    def validate_input(self, raw_input: dict[str, Any]) -> ReadToolInput:
        payload = dict(raw_input or {})
        normalized = {
            "file_path": payload.get("file_path") or payload.get("path") or payload.get("file"),
            "file_paths": list(payload.get("file_paths") or payload.get("paths") or []),
            "start_line": payload.get("start_line") or payload.get("from_line"),
            "end_line": payload.get("end_line") or payload.get("to_line"),
            "max_lines": payload.get("max_lines") or payload.get("limit") or 400,
            "max_files": payload.get("max_files") or 6,
        }
        if not normalized["file_path"] and normalized["file_paths"]:
            normalized["file_path"] = normalized["file_paths"][0]
        return ReadToolInput.model_validate(normalized)

    def is_concurrency_safe(self, parsed_input: Any) -> bool:
        return True

    async def execute(self, parsed_input: ReadToolInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        file_paths = [item for item in parsed_input.file_paths if str(item or "").strip()]
        if len(file_paths) > 1:
            if self._read_many_tool is None:
                raise ValueError("Batch file reading is not available in this runtime.")
            result = await self._read_many_tool.execute(
                file_paths=file_paths,
                start_line=parsed_input.start_line,
                end_line=parsed_input.end_line,
                max_lines=parsed_input.max_lines,
                max_files=parsed_input.max_files,
            )
            return _result_to_payload(result)

        if self._read_tool is None or not parsed_input.file_path:
            raise ValueError("Read requires file_path or file_paths.")
        result = await self._read_tool.execute(
            file_path=parsed_input.file_path,
            start_line=parsed_input.start_line,
            end_line=parsed_input.end_line,
            max_lines=parsed_input.max_lines,
        )
        return _result_to_payload(result)


class CanonicalGlobTool(RuntimeTool):
    name = "Glob"
    description = (
        "按文件名或路径模式枚举项目文件。适合在不知道准确路径时发现路由文件、控制器、服务、配置、"
        "测试、模板、迁移脚本、语言入口文件和特定扩展名文件。\n\n"
        "用法：\n"
        "- path 是相对项目根目录的搜索目录，默认 \".\"。\n"
        "- pattern 是 glob 模式，例如 \"**/*.py\"、\"src/**/*.java\"、\"**/*Controller*\"、\"**/*.xml\"。\n"
        "- recursive 控制是否递归子目录，默认递归。\n"
        "- max_results 控制最多返回的文件数量，避免结果过大。\n\n"
        "使用建议：\n"
        "- 需要找文件名、扩展名、目录结构时使用 Glob。\n"
        "- 找到候选文件后，用 Read 阅读内容。\n"
        "- 需要按内容查找时使用 Grep，不要用 Glob 代替内容搜索。\n"
        "- 如果一次 Glob 返回太多结果，缩小 path 或 pattern。\n\n"
        "审计要求：\n"
        "- 当你需要继续发现相关文件、扩大审计范围或定位未知文件路径时，必须调用 Glob、Grep、Read "
        "或其它合适工具。\n"
        "- 不要只说明“接下来查找相关文件”，必须实际调用工具。"
    )
    input_model = GlobToolInput

    def __init__(self, *, list_tool: AgentTool):
        self._list_tool = list_tool

    def validate_input(self, raw_input: dict[str, Any]) -> GlobToolInput:
        payload = dict(raw_input or {})
        requested_max_results = _coerce_positive_int(
            payload.get("max_results") or payload.get("max_files"),
            GLOB_DEFAULT_MAX_RESULTS,
        )
        normalized = {
            "path": payload.get("path") or payload.get("directory") or ".",
            "pattern": payload.get("pattern") or payload.get("glob"),
            "recursive": payload.get("recursive", True),
            "max_results": requested_max_results,
            "timeout_seconds": payload.get("timeout_seconds") or RUNTIME_SEARCH_TOOL_TIMEOUT_SECONDS,
        }
        return GlobToolInput.model_validate(normalized)

    def is_concurrency_safe(self, parsed_input: Any) -> bool:
        return True

    def execution_timeout_seconds(self, parsed_input: Any = None, context: ToolExecutionContext | None = None) -> float | None:
        del context
        return min(RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS, max(1, int(parsed_input.timeout_seconds))) + 2

    async def execute(self, parsed_input: GlobToolInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        requested_max_results = parsed_input.max_results
        applied_max_results = min(max(1, int(parsed_input.max_results)), GLOB_HARD_MAX_RESULTS)
        result = await self._list_tool.execute(
            directory=parsed_input.path,
            pattern=parsed_input.pattern,
            recursive=parsed_input.recursive,
            max_files=applied_max_results,
            timeout_seconds=parsed_input.timeout_seconds,
        )
        payload = _result_to_payload(result)
        return _append_truncation_hint(payload, requested=requested_max_results, limit=applied_max_results)


class CanonicalGrepTool(RuntimeTool):
    name = "Grep"
    description = (
        "在项目代码和配置文本中搜索关键字或正则表达式。底层语义等价于高效代码搜索，适合追踪路由、"
        "函数名、参数名、权限校验、危险 API、source、sink、配置项和跨文件调用关系。\n\n"
        "用法：\n"
        "- pattern 是要搜索的关键字或正则表达式。\n"
        "- path 可选，用于限制搜索目录；不提供时默认从项目根目录搜索。\n"
        "- glob 可选，用于限制文件类型或路径范围，例如 \"*.py\"、\"**/*.java\"、\"src/**/*.ts\"。\n"
        "- case_sensitive 控制是否大小写敏感，默认不敏感。\n"
        "- is_regex=true 时 pattern 按正则处理；普通关键字搜索保持 is_regex=false。\n"
        "- max_results 控制最多返回的匹配数量，避免一次搜索结果过大。\n\n"
        "使用建议：\n"
        "- 搜索任务优先使用 Grep，不要通过 PowerShell 手写 grep/rg/findstr，除非 Grep 无法表达该查询。\n"
        "- 已知标识符、接口路径、参数名、函数名、类名、配置 key 时，先用 Grep 定位引用，再用 Read 阅读关键文件。\n"
        "- 追踪漏洞链时，用 Grep 查找 source 输入点、sink 调用点、鉴权/权限判断、过滤/转义函数、跨层 service/model 调用。\n\n"
        "审计要求：\n"
        "- 当你需要继续搜索、追踪、确认引用、查找调用链或补齐证据时，必须调用 Grep、Read、Glob 或其它合适工具。\n"
        "- 不要只回复“我将继续搜索/继续追踪/下一步检查”，继续就必须实际发起工具调用。"
    )
    input_model = GrepToolInput

    def __init__(self, *, search_tool: AgentTool):
        self._search_tool = search_tool

    def validate_input(self, raw_input: dict[str, Any]) -> GrepToolInput:
        payload = dict(raw_input or {})
        requested_max_results = _coerce_positive_int(
            payload.get("max_results") or payload.get("limit"),
            GREP_DEFAULT_MAX_RESULTS,
        )
        normalized = {
            "pattern": payload.get("pattern") or payload.get("query") or payload.get("keyword"),
            "path": payload.get("path") or payload.get("directory"),
            "glob": payload.get("glob") or payload.get("file_pattern"),
            "case_sensitive": payload.get("case_sensitive", False),
            "max_results": requested_max_results,
            "is_regex": payload.get("is_regex", False),
            "timeout_seconds": payload.get("timeout_seconds") or RUNTIME_SEARCH_TOOL_TIMEOUT_SECONDS,
        }
        return GrepToolInput.model_validate(normalized)

    def is_concurrency_safe(self, parsed_input: Any) -> bool:
        return True

    def execution_timeout_seconds(self, parsed_input: Any = None, context: ToolExecutionContext | None = None) -> float | None:
        del context
        return min(RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS, max(1, int(parsed_input.timeout_seconds))) + 2

    async def execute(self, parsed_input: GrepToolInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        requested_max_results = parsed_input.max_results
        applied_max_results = min(max(1, int(parsed_input.max_results)), GREP_HARD_MAX_RESULTS)
        result = await self._search_tool.execute(
            keyword=parsed_input.pattern,
            file_pattern=parsed_input.glob,
            directory=parsed_input.path,
            case_sensitive=parsed_input.case_sensitive,
            max_results=applied_max_results,
            is_regex=parsed_input.is_regex,
            timeout_seconds=parsed_input.timeout_seconds,
        )
        payload = _result_to_payload(result)
        return _append_truncation_hint(payload, requested=requested_max_results, limit=applied_max_results)


class CanonicalWriteTool(RuntimeTool):
    name = "Write"
    description = (
        "为当前审计会话写入文本产物。"
        "调用时必须直接传 path 和 content，可选 overwrite；不要使用 raw_input、裸字符串或数组。"
        "托管输出建议写入 .auditai/ 目录。"
        "启用护栏时，写入源码文件或项目根目录外的位置需要用户明确批准。"
    )
    input_model = WriteToolInput

    def __init__(self, *, session_store=None, project_root: str | None = None):
        self._session_store = session_store
        self._project_root = str(project_root or "").strip() or None

    def _resolve_project_root(self, context: ToolExecutionContext) -> Path:
        if self._project_root:
            return Path(self._project_root).resolve()
        payload = dict(context.recon_payload or {})
        if not payload and getattr(context.session, "recon_payload", None):
            payload = dict(context.session.recon_payload or {})
        project_info = payload.get("project_info") if isinstance(payload, dict) else {}
        workspace_root = str((project_info or {}).get("workspace_root") or "").strip()
        if not workspace_root:
            raise ValueError("Missing workspace root for write tool")
        return Path(workspace_root).resolve()

    def _guardrails_enabled(self, *, context: ToolExecutionContext) -> bool:
        if self._session_store is None:
            return False
        runtime_state = self._session_store.load_runtime_state(context.session_id)
        return is_guardrails_enabled(runtime_state)

    def _has_matching_approval(
        self,
        *,
        context: ToolExecutionContext,
        project_root: Path,
        resolved_path: Path,
        guardrail_code: str,
    ) -> bool:
        if self._session_store is None:
            return False
        runtime_state = self._session_store.load_runtime_state(context.session_id)
        return has_write_approval(
            runtime_state,
            project_root=project_root,
            resolved_path=resolved_path,
            guardrail_code=guardrail_code,
        )

    def _consume_matching_approval(
        self,
        *,
        context: ToolExecutionContext,
        project_root: Path,
        resolved_path: Path,
        guardrail_code: str,
    ) -> None:
        if self._session_store is None:
            return
        runtime_state = self._session_store.load_runtime_state(context.session_id)
        approval = consume_write_approval(
            runtime_state,
            project_root=project_root,
            resolved_path=resolved_path,
            guardrail_code=guardrail_code,
        )
        if approval and str(approval.get("scope") or "") == APPROVAL_SCOPE_SINGLE_USE:
            self._session_store.replace_runtime_state(context.session_id, runtime_state)

    @staticmethod
    def register_approval(
        runtime_state,
        *,
        path: str,
        guardrail_code: str,
        tool_call_id: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        return register_write_approval(
            runtime_state,
            path=path,
            guardrail_code=guardrail_code,
            tool_call_id=tool_call_id,
            scope=scope,
        )

    async def check_permission(
        self,
        parsed_input: WriteToolInput,
        context: ToolExecutionContext,
    ) -> ToolPermissionDecision:
        project_root = self._resolve_project_root(context)
        requested_path = str(parsed_input.path or "").strip()
        candidate = Path(requested_path)
        guardrails_enabled = self._guardrails_enabled(context=context)

        if candidate.is_absolute():
            if guardrails_enabled and self._has_matching_approval(
                context=context,
                project_root=project_root,
                resolved_path=candidate.resolve(),
                guardrail_code="absolute_path_requires_approval",
            ):
                return ToolPermissionDecision(allowed=True, source="tool_guardrail", mode="allow")
            if guardrails_enabled:
                return ToolPermissionDecision(
                    allowed=False,
                    source="tool_guardrail",
                    mode="ask",
                    reason="Writing to an absolute path requires explicit approval.",
                    guardrail_code="absolute_path_requires_approval",
                )
            return ToolPermissionDecision(allowed=True, source="tool_guardrail", mode="allow")

        resolved_path = (project_root / candidate).resolve()
        try:
            resolved_path.relative_to(project_root)
        except ValueError:
            if guardrails_enabled and self._has_matching_approval(
                context=context,
                project_root=project_root,
                resolved_path=resolved_path,
                guardrail_code="outside_project_root_requires_approval",
            ):
                return ToolPermissionDecision(allowed=True, source="tool_guardrail", mode="allow")
            if guardrails_enabled:
                return ToolPermissionDecision(
                    allowed=False,
                    source="tool_guardrail",
                    mode="ask",
                    reason="Writing outside the project root requires explicit approval.",
                    guardrail_code="outside_project_root_requires_approval",
                )
            return ToolPermissionDecision(allowed=True, source="tool_guardrail", mode="allow")

        artifact_root = (project_root / ".auditai").resolve()
        try:
            resolved_path.relative_to(artifact_root)
        except ValueError:
            if guardrails_enabled and self._has_matching_approval(
                context=context,
                project_root=project_root,
                resolved_path=resolved_path,
                guardrail_code="source_write_requires_approval",
            ):
                return ToolPermissionDecision(allowed=True, source="tool_guardrail", mode="allow")
            if guardrails_enabled:
                return ToolPermissionDecision(
                    allowed=False,
                    source="tool_guardrail",
                    mode="ask",
                    reason="写入源码文件需要明确批准。生成的审计产物请写入 .auditai/。",
                    guardrail_code="source_write_requires_approval",
                )

        if resolved_path.exists() and parsed_input.overwrite and guardrails_enabled and self._has_matching_approval(
            context=context,
            project_root=project_root,
            resolved_path=resolved_path,
            guardrail_code="overwrite_existing_requires_approval",
        ):
            return ToolPermissionDecision(allowed=True, source="tool_guardrail", mode="allow")

        if resolved_path.exists() and not parsed_input.overwrite:
            return ToolPermissionDecision(
                allowed=False,
                source="tool_guardrail",
                mode="deny",
                reason="Target file already exists. Re-run with overwrite=true to replace an existing artifact.",
                guardrail_code="artifact_exists_requires_overwrite",
            )

        if resolved_path.exists() and parsed_input.overwrite and guardrails_enabled:
            return ToolPermissionDecision(
                allowed=False,
                source="tool_guardrail",
                mode="ask",
                reason="Overwriting an existing file requires explicit approval while guardrails are enabled.",
                guardrail_code="overwrite_existing_requires_approval",
            )

        return ToolPermissionDecision(allowed=True, source="tool_guardrail", mode="allow")

    async def execute(self, parsed_input: WriteToolInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        project_root = self._resolve_project_root(context)
        requested_path = Path(str(parsed_input.path or "").strip())
        resolved_path = requested_path.resolve() if requested_path.is_absolute() else (project_root / requested_path).resolve()
        guardrails_enabled = self._guardrails_enabled(context=context)
        if guardrails_enabled:
            if requested_path.is_absolute():
                self._consume_matching_approval(
                    context=context,
                    project_root=project_root,
                    resolved_path=resolved_path,
                    guardrail_code="absolute_path_requires_approval",
                )
            else:
                try:
                    resolved_path.relative_to(project_root)
                except ValueError:
                    self._consume_matching_approval(
                        context=context,
                        project_root=project_root,
                        resolved_path=resolved_path,
                        guardrail_code="outside_project_root_requires_approval",
                    )
                else:
                    artifact_root = (project_root / ".auditai").resolve()
                    try:
                        resolved_path.relative_to(artifact_root)
                    except ValueError:
                        self._consume_matching_approval(
                            context=context,
                            project_root=project_root,
                            resolved_path=resolved_path,
                            guardrail_code="source_write_requires_approval",
                        )
                    if resolved_path.exists() and parsed_input.overwrite:
                        self._consume_matching_approval(
                            context=context,
                            project_root=project_root,
                            resolved_path=resolved_path,
                            guardrail_code="overwrite_existing_requires_approval",
                        )
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path.write_text(parsed_input.content, encoding="utf-8")
        artifact_root = (project_root / ".auditai").resolve()
        is_managed_output = False
        try:
            resolved_path.relative_to(artifact_root)
            is_managed_output = True
        except ValueError:
            is_managed_output = False
        return ToolExecutionPayload(
            content=f"Wrote audit artifact to {resolved_path}",
            output_payload={
                "path": parsed_input.path,
                "resolved_path": str(resolved_path),
                "bytes_written": len(parsed_input.content.encode("utf-8")),
                "artifact_type": "managed_output" if is_managed_output else "project_write",
                "overwrite": parsed_input.overwrite,
            },
            metadata={"managed_output": is_managed_output},
        )


def build_runtime_tool_registry(
    *,
    session_store,
    agent_tools: dict[str, AgentTool],
    agent_type: str,
    user_id: str | None = None,
    include_finding_finalizer: bool = True,
    include_report_finalizer: bool = False,
) -> ToolRegistry:
    tools: list[RuntimeTool] = []

    read_tool = agent_tools.get("read_file")
    if isinstance(read_tool, AgentTool):
        read_many_tool = agent_tools.get("read_many_files")
        tools.append(
            CanonicalReadTool(
                read_tool=read_tool,
                read_many_tool=read_many_tool if isinstance(read_many_tool, AgentTool) else None,
            )
        )

    list_tool = agent_tools.get("list_files")
    if isinstance(list_tool, AgentTool):
        tools.append(CanonicalGlobTool(list_tool=list_tool))

    search_tool = agent_tools.get("search_code")
    if isinstance(search_tool, AgentTool):
        tools.append(CanonicalGrepTool(search_tool=search_tool))

    project_root = _infer_project_root(agent_tools)
    tools.append(CanonicalWriteTool(session_store=session_store, project_root=project_root))
    shell_backend = agent_tools.get("sandbox_exec") if isinstance(agent_tools.get("sandbox_exec"), AgentTool) else None
    if project_root:
        bash_executable = detect_bash_executable()
        if bash_executable or shell_backend is not None:
            tools.append(
                BashRuntimeTool(
                    project_root=project_root,
                    backend_tool=shell_backend,
                    executable=bash_executable,
                    session_store=session_store,
                )
            )
        if is_powershell_runtime_tool_enabled():
            powershell_executable = detect_powershell_executable()
            if powershell_executable or shell_backend is not None:
                tools.append(
                    PowerShellRuntimeTool(
                        project_root=project_root,
                        backend_tool=shell_backend,
                        executable=powershell_executable,
                        session_store=session_store,
                    )
                )

    tools.append(
        RuntimeSkillTool(
            session_store=session_store,
            agent_type=agent_type,
            user_id=user_id,
        )
    )
    if str(agent_type or "").strip() == "finding" and include_finding_finalizer:
        tools.append(FinalizeFindingTool())
    if str(agent_type or "").strip() == "finding" and include_report_finalizer:
        tools.append(FinalizeVulnerabilityReportsTool())
    if str(agent_type or "").strip() == "triage":
        tools.extend(
            [
                GetTriageBatchTool(project_root=project_root),
                GetScanFindingTool(project_root=project_root),
                FinalizeTriageBatchTool(project_root=project_root),
                FinalizeTriageTool(project_root=project_root),
            ]
        )
    tools.extend(
        [
            TodoWriteRuntimeTool(session_store),
            AskUserRuntimeTool(session_store),
            EnterPlanModeRuntimeTool(session_store),
            ExitPlanModeRuntimeTool(session_store),
        ]
    )

    registry = ToolRegistry(tools)
    if registry.has_deferred_tools():
        registry.register(ToolSearchRuntimeTool(session_store=session_store, registry_getter=lambda: registry))
    return registry
