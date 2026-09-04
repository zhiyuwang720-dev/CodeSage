"""Write 工具(P4 归一: 原 canonical Write 的直实现, 保留 guardrails/权限逻辑)。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.services.contracts.models import ToolExecutionPayload
from app.services.runtime_core.permission_runtime import ToolPermissionDecision
from app.services.runtime_core.runtime_guardrails import (
    APPROVAL_SCOPE_SINGLE_USE,
    consume_write_approval,
    has_write_approval,
    is_guardrails_enabled,
    register_write_approval,
)
from app.services.tooling.runtime import RuntimeTool, ToolExecutionContext


class WriteToolInput(BaseModel):
    path: str = Field(description="Required target path relative to the project root. Managed outputs should go under .auditai/.")
    content: str = Field(description="Required text content to write.")
    overwrite: bool = Field(default=False, description="Whether to overwrite an existing file.")


class WriteRuntimeTool(RuntimeTool):
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

    @property
    def project_root(self) -> str | None:
        return self._project_root

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
