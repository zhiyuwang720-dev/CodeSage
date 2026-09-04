from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, AsyncGenerator, Awaitable, Callable, Literal

from pydantic import BaseModel, ValidationError

from app.models.audit_session import AuditCheckpointType, AuditToolCallStatus
from app.services.runtime_core.permission_runtime import RuntimePermissionRuntime, ToolPermissionDecision
from app.services.contracts.models import (
    ToolCallRequest,
    ToolExecutionPayload,
    ToolExecutionRecord,
)

InterruptBehavior = Literal["cancel", "block"]
DEFAULT_RUNTIME_TOOL_TIMEOUT_SECONDS = 120
RUNTIME_SEARCH_TOOL_TIMEOUT_SECONDS = 45
RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS = 120
RUNTIME_TOOL_TIMEOUT_HINT = "工具执行超时：请缩小 path/glob/pattern 后重试。"


# 06-P2: RuntimeTool ABC 与 ToolExecutionContext 迁往契约层; 引擎仍经本模块再导出,
# 既有 `from ...tooling.runtime import RuntimeTool` 引用点不受影响。
from app.services.contracts.tools import RuntimeTool, ToolExecutionContext


def match_runtime_event_hooks(hook_config: dict[str, Any], *, event_name: str, tool_name: str) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for entry in hook_config.get(event_name) or []:
        matcher = str(entry.get("matcher") or "").strip()
        if matcher not in {"", "*", tool_name}:
            continue
        matched.append(dict(entry))
    return matched


class _ConfiguredRuntimeTool(RuntimeTool):
    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_model: type[BaseModel] | None,
        execute: Callable[[Any, ToolExecutionContext], Awaitable[ToolExecutionPayload]],
        validate_input: Callable[[dict[str, Any]], Any] | None = None,
        aliases: list[str] | tuple[str, ...] | None = None,
        search_hint: str | None = None,
        is_enabled: Callable[[], bool] | None = None,
        is_concurrency_safe: Callable[[Any], bool] | None = None,
        concurrency_key: Callable[[Any], str | None] | None = None,
        is_read_only: Callable[[Any], bool] | None = None,
        is_destructive: Callable[[Any], bool] | None = None,
        interrupt_behavior: Callable[[], InterruptBehavior] | None = None,
        requires_user_interaction: Callable[[], bool] | None = None,
        should_defer: bool = False,
        always_load: bool = False,
        check_permission: Callable[[Any, ToolExecutionContext], Awaitable[ToolPermissionDecision]] | None = None,
        user_facing_name: Callable[[Any | None], str] | None = None,
    ):
        self.name = name
        self.description = description
        self.input_model = input_model
        self.aliases = [alias for alias in aliases or [] if str(alias or "").strip()]
        self.search_hint = str(search_hint).strip() or None if search_hint is not None else None
        self.should_defer = bool(should_defer)
        self.always_load = bool(always_load)
        self._execute_fn = execute
        self._validate_input_fn = validate_input
        self._is_enabled_fn = is_enabled
        self._is_concurrency_safe_fn = is_concurrency_safe
        self._concurrency_key_fn = concurrency_key
        self._is_read_only_fn = is_read_only
        self._is_destructive_fn = is_destructive
        self._interrupt_behavior_fn = interrupt_behavior
        self._requires_user_interaction_fn = requires_user_interaction
        self._check_permission_fn = check_permission
        self._user_facing_name_fn = user_facing_name

    def validate_input(self, raw_input: dict[str, Any]) -> Any:
        if self._validate_input_fn is not None:
            return self._validate_input_fn(raw_input)
        return super().validate_input(raw_input)

    def is_enabled(self) -> bool:
        if self._is_enabled_fn is None:
            return super().is_enabled()
        return bool(self._is_enabled_fn())

    def is_concurrency_safe(self, parsed_input: Any = None) -> bool:
        if self._is_concurrency_safe_fn is None:
            return super().is_concurrency_safe(parsed_input)
        return bool(self._is_concurrency_safe_fn(parsed_input))

    def concurrency_key(self, parsed_input: Any = None) -> str | None:
        if self._concurrency_key_fn is None:
            return super().concurrency_key(parsed_input)
        return self._concurrency_key_fn(parsed_input)

    def is_read_only(self, parsed_input: Any = None) -> bool:
        if self._is_read_only_fn is None:
            return super().is_read_only(parsed_input)
        return bool(self._is_read_only_fn(parsed_input))

    def is_destructive(self, parsed_input: Any = None) -> bool:
        if self._is_destructive_fn is None:
            return super().is_destructive(parsed_input)
        return bool(self._is_destructive_fn(parsed_input))

    def interrupt_behavior(self) -> InterruptBehavior:
        if self._interrupt_behavior_fn is None:
            return super().interrupt_behavior()
        behavior = self._interrupt_behavior_fn()
        return "cancel" if behavior == "cancel" else "block"

    def requires_user_interaction(self) -> bool:
        if self._requires_user_interaction_fn is None:
            return super().requires_user_interaction()
        return bool(self._requires_user_interaction_fn())

    async def check_permission(self, parsed_input: Any, context: ToolExecutionContext) -> ToolPermissionDecision:
        if self._check_permission_fn is None:
            return await super().check_permission(parsed_input, context)
        return await self._check_permission_fn(parsed_input, context)

    async def execute(self, parsed_input: Any, context: ToolExecutionContext) -> ToolExecutionPayload:
        return await self._execute_fn(parsed_input, context)

    def user_facing_name(self, raw_input: Any | None = None) -> str:
        if self._user_facing_name_fn is None:
            return super().user_facing_name(raw_input)
        return str(self._user_facing_name_fn(raw_input))


def build_runtime_tool(
    *,
    name: str,
    description: str,
    input_model: type[BaseModel] | None = None,
    execute: Callable[[Any, ToolExecutionContext], Awaitable[ToolExecutionPayload]],
    validate_input: Callable[[dict[str, Any]], Any] | None = None,
    aliases: list[str] | tuple[str, ...] | None = None,
    search_hint: str | None = None,
    is_enabled: Callable[[], bool] | None = None,
    is_concurrency_safe: Callable[[Any], bool] | None = None,
    concurrency_key: Callable[[Any], str | None] | None = None,
    is_read_only: Callable[[Any], bool] | None = None,
    is_destructive: Callable[[Any], bool] | None = None,
    interrupt_behavior: Callable[[], InterruptBehavior] | None = None,
    requires_user_interaction: Callable[[], bool] | None = None,
    should_defer: bool = False,
    always_load: bool = False,
    check_permission: Callable[[Any, ToolExecutionContext], Awaitable[ToolPermissionDecision]] | None = None,
    user_facing_name: Callable[[Any | None], str] | None = None,
) -> RuntimeTool:
    return _ConfiguredRuntimeTool(
        name=name,
        description=description,
        input_model=input_model,
        execute=execute,
        validate_input=validate_input,
        aliases=aliases,
        search_hint=search_hint,
        is_enabled=is_enabled,
        is_concurrency_safe=is_concurrency_safe,
        concurrency_key=concurrency_key,
        is_read_only=is_read_only,
        is_destructive=is_destructive,
        interrupt_behavior=interrupt_behavior,
        requires_user_interaction=requires_user_interaction,
        should_defer=should_defer,
        always_load=always_load,
        check_permission=check_permission,
        user_facing_name=user_facing_name,
    )


class ToolRegistry:
    def __init__(self, tools: list[RuntimeTool] | None = None):
        self._tools: dict[str, RuntimeTool] = {}
        self._aliases: dict[str, str] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: RuntimeTool) -> None:
        if not tool.name:
            raise ValueError("Runtime tools must define a name")
        if tool.name in self._aliases:
            raise ValueError(f"Tool name conflicts with existing alias: {tool.name}")
        self._tools[tool.name] = tool
        for alias in tool.aliases or []:
            normalized = str(alias or "").strip()
            if not normalized:
                continue
            if normalized == tool.name:
                continue
            if normalized in self._tools and self._tools[normalized] is not tool:
                raise ValueError(f"Tool alias conflicts with existing tool name: {normalized}")
            existing_name = self._aliases.get(normalized)
            if existing_name is not None and existing_name != tool.name:
                raise ValueError(f"Tool alias conflicts with existing alias: {normalized}")
            self._aliases[normalized] = tool.name

    def register_many(self, tools: list[RuntimeTool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> RuntimeTool | None:
        canonical_name = self._aliases.get(name, name)
        return self._tools.get(canonical_name)

    def all_tools(self) -> list[RuntimeTool]:
        return list(self._tools.values())

    def enabled_tools(self) -> list[RuntimeTool]:
        enabled: list[RuntimeTool] = []
        for tool in self._tools.values():
            try:
                if not tool.is_enabled():
                    continue
            except Exception:
                continue
            enabled.append(tool)
        return enabled

    @staticmethod
    def _is_deferred(tool: RuntimeTool) -> bool:
        if tool.always_load:
            return False
        return bool(tool.should_defer)

    def resolve_active_tool_names(self, active_tool_names: list[str] | tuple[str, ...] | set[str] | None = None) -> list[str]:
        enabled = self.enabled_tools()
        enabled_by_name = {tool.name: tool for tool in enabled}
        active: set[str] = set()
        for tool in enabled:
            if not self._is_deferred(tool):
                active.add(tool.name)
        for raw_name in active_tool_names or []:
            canonical = self._aliases.get(str(raw_name), str(raw_name))
            if canonical in enabled_by_name:
                active.add(canonical)
        return [tool.name for tool in self._tools.values() if tool.name in active and tool.name in enabled_by_name]

    def deferred_tools(self, active_tool_names: list[str] | tuple[str, ...] | set[str] | None = None) -> list[RuntimeTool]:
        active = set(self.resolve_active_tool_names(active_tool_names))
        return [tool for tool in self.enabled_tools() if self._is_deferred(tool) and tool.name not in active]

    def has_deferred_tools(self, active_tool_names: list[str] | tuple[str, ...] | set[str] | None = None) -> bool:
        return bool(self.deferred_tools(active_tool_names))

    def describe_tools(self, active_tool_names: list[str] | tuple[str, ...] | set[str] | None = None) -> list[dict[str, Any]]:
        active = set(self.resolve_active_tool_names(active_tool_names))
        described: list[dict[str, Any]] = []
        for tool in self.enabled_tools():
            if self._is_deferred(tool) and tool.name not in active:
                continue
            described.append(tool.describe())
        return described


@dataclass(slots=True)
class _PreparedToolCall:
    request: ToolCallRequest
    tool: RuntimeTool | None
    parsed_input: Any = None
    is_concurrency_safe: bool = False
    concurrency_key: str | None = None
    validation_error: str | None = None


@dataclass(slots=True)
class ToolExecutionUpdate:
    kind: Literal["progress", "record", "context"]
    tool_use_id: str | None = None
    tool_name: str | None = None
    progress_payload: dict[str, Any] | None = None
    record: ToolExecutionRecord | None = None
    new_context: dict[str, Any] | None = None


def _format_validation_error(tool_name: str, exc: ValidationError) -> str:
    issues: list[str] = []
    for error in exc.errors():
        loc = '.'.join(str(part) for part in error.get("loc") or []) or "input"
        msg = str(error.get("msg") or "invalid value")
        issues.append(f"{loc}: {msg}")
    if not issues:
        return f"Invalid input for tool '{tool_name}'."
    return f"Invalid input for tool '{tool_name}': " + '; '.join(issues)


def _classify_execution_error_kind(error: BaseException) -> str:
    if isinstance(error, asyncio.CancelledError):
        return "interrupted"
    name = getattr(error, "__class__", type(error)).__name__.lower()
    if "permission" in name:
        return "permission_error"
    if "timeout" in name:
        return "timeout_error"
    if "shell" in name:
        return "shell_error"
    return "execution_error"


class ToolOrchestrator:
    def __init__(
        self,
        *,
        session_store,
        tool_registry: ToolRegistry,
        agent_type: str = "review:security",
        permission_runtime: RuntimePermissionRuntime | None = None,
        default_tool_timeout_seconds: float | None = DEFAULT_RUNTIME_TOOL_TIMEOUT_SECONDS,
    ):
        self._session_store = session_store
        self._tool_registry = tool_registry
        self._agent_type = agent_type
        self._permission_runtime = permission_runtime or RuntimePermissionRuntime(session_store=session_store, agent_type=agent_type)
        self._default_tool_timeout_seconds = default_tool_timeout_seconds

    async def execute_tool_calls(
        self,
        *,
        session_id: str,
        turn_id: str,
        tool_calls: list[ToolCallRequest],
        session: Any = None,
        recon_payload: dict[str, Any] | None = None,
    ) -> list[ToolExecutionRecord]:
        records: list[ToolExecutionRecord] = []
        async for update in self.stream_tool_call_updates(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=tool_calls,
            session=session,
            recon_payload=recon_payload,
        ):
            if update.kind == "record" and update.record is not None:
                records.append(update.record)
        return records

    async def stream_tool_call_updates(
        self,
        *,
        session_id: str,
        turn_id: str,
        tool_calls: list[ToolCallRequest],
        session: Any = None,
        recon_payload: dict[str, Any] | None = None,
        initial_context: dict[str, Any] | None = None,
    ) -> AsyncGenerator[ToolExecutionUpdate, None]:
        executor = self.build_streaming_executor(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=tool_calls,
            session=session,
            recon_payload=recon_payload,
            initial_context=initial_context,
        )
        async for update in executor.get_remaining_updates():
            yield update

    def build_streaming_executor(
        self,
        *,
        session_id: str,
        turn_id: str,
        tool_calls: list[ToolCallRequest],
        session: Any = None,
        recon_payload: dict[str, Any] | None = None,
        initial_context: dict[str, Any] | None = None,
    ) -> "StreamingToolExecutor":
        prepared = [self._prepare_call(tool_call) for tool_call in tool_calls]
        return StreamingToolExecutor(
            orchestrator=self,
            session_id=session_id,
            turn_id=turn_id,
            prepared_calls=prepared,
            session=session,
            recon_payload=dict(recon_payload or {}),
            initial_context=dict(initial_context or {}),
        )

    def _prepare_call(self, request: ToolCallRequest) -> _PreparedToolCall:
        tool = self._tool_registry.get(request.name)
        if tool is None:
            return _PreparedToolCall(request=request, tool=None)
        try:
            parsed_input = tool.validate_input(request.input)
        except ValidationError as exc:
            return _PreparedToolCall(request=request, tool=tool, validation_error=_format_validation_error(tool.name, exc))

        try:
            is_concurrency_safe = bool(tool.is_concurrency_safe(parsed_input))
        except Exception:
            is_concurrency_safe = False
        try:
            raw_concurrency_key = tool.concurrency_key(parsed_input) if is_concurrency_safe else None
            concurrency_key = str(raw_concurrency_key).strip() or None if raw_concurrency_key is not None else None
        except Exception:
            concurrency_key = None
        return _PreparedToolCall(
            request=request,
            tool=tool,
            parsed_input=parsed_input,
            is_concurrency_safe=is_concurrency_safe,
            concurrency_key=concurrency_key,
        )

    @staticmethod
    def _partition_batches(prepared_calls: list[_PreparedToolCall]) -> list[tuple[bool, list[_PreparedToolCall]]]:
        batches: list[tuple[bool, list[_PreparedToolCall]]] = []
        active_keys: set[str] = set()
        for prepared_call in prepared_calls:
            if prepared_call.is_concurrency_safe:
                key = prepared_call.concurrency_key
                can_join_batch = bool(batches and batches[-1][0] and (not key or key not in active_keys))
                if can_join_batch:
                    batches[-1][1].append(prepared_call)
                    if key:
                        active_keys.add(key)
                    continue
                batches.append((True, [prepared_call]))
                active_keys = {key} if key else set()
                continue
            batches.append((False, [prepared_call]))
            active_keys = set()
        return batches

    async def _execute_prepared_call(
        self,
        prepared_call: _PreparedToolCall,
        *,
        session_id: str,
        turn_id: str,
        session: Any,
        recon_payload: dict[str, Any],
        progress_callback: Callable[[ToolExecutionUpdate], None] | None = None,
        skip_execution_reason: str | None = None,
    ) -> ToolExecutionRecord:
        request = prepared_call.request
        tool_call_id = self._session_store.start_tool_call(
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=request.id,
            tool_name=request.name,
            input_payload=request.input,
            is_concurrency_safe=prepared_call.is_concurrency_safe,
        )
        started = perf_counter()

        if prepared_call.tool is None:
            return self._finalize_error_record(
                tool_call_id=tool_call_id,
                request=request,
                status=AuditToolCallStatus.MISSING.value,
                is_concurrency_safe=prepared_call.is_concurrency_safe,
                started=started,
                message=f"Unknown tool: {request.name}",
            )

        if prepared_call.validation_error is not None:
            return self._finalize_error_record(
                tool_call_id=tool_call_id,
                request=request,
                status=AuditToolCallStatus.INVALID.value,
                is_concurrency_safe=prepared_call.is_concurrency_safe,
                started=started,
                message=prepared_call.validation_error,
                metadata={"error_kind": "validation_error"},
                lifecycle={"requested_tool_name": request.name},
            )

        if skip_execution_reason is not None:
            return self._finalize_error_record(
                tool_call_id=tool_call_id,
                request=request,
                status=AuditToolCallStatus.FAILED.value,
                is_concurrency_safe=prepared_call.is_concurrency_safe,
                started=started,
                message=skip_execution_reason,
                metadata={"error_kind": "interrupted"},
                lifecycle={"requested_tool_name": request.name, "resolved_tool_name": prepared_call.tool.name},
            )

        progress_events: list[dict[str, Any]] = []
        lifecycle: dict[str, Any] = {
            "requested_tool_name": request.name,
            "resolved_tool_name": prepared_call.tool.name,
        }
        context = ToolExecutionContext(
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=request.id,
            tool_call_id=tool_call_id,
            agent_type=self._agent_type,
            session=session,
            recon_payload=dict(recon_payload or {}),
            metadata={"lifecycle": lifecycle},
            on_progress=lambda payload: self._record_progress_event(
                session_id=session_id,
                turn_id=turn_id,
                tool_use_id=request.id,
                tool_name=prepared_call.tool.name,
                progress_events=progress_events,
                payload=payload,
                progress_callback=progress_callback,
            ),
        )
        if request.name != prepared_call.tool.name:
            self._session_store.create_checkpoint(
                session_id=session_id,
                turn_id=turn_id,
                checkpoint_type=AuditCheckpointType.AUTO,
                state_payload={
                    "kind": "runtime_tool_alias_resolution",
                    "requested_tool_name": request.name,
                    "resolved_tool_name": prepared_call.tool.name,
                    "tool_use_id": request.id,
                },
            )

        runtime_permission = self._permission_runtime.evaluate_tool_use(tool_name=request.name, context=context)
        if not runtime_permission.allowed:
            self._emit_hook_event(
                event_name="PermissionDenied",
                context=context,
                tool_name=request.name,
                payload={"reason": runtime_permission.reason, "source": runtime_permission.source, "mode": runtime_permission.mode},
            )
            return self._finalize_error_record(
                tool_call_id=tool_call_id,
                request=request,
                status=AuditToolCallStatus.DENIED.value,
                is_concurrency_safe=prepared_call.is_concurrency_safe,
                started=started,
                message=runtime_permission.reason or "Tool permission denied",
                output_payload={
                    "permission_source": runtime_permission.source,
                    "permission_mode": runtime_permission.mode,
                    "permission_reason": runtime_permission.reason,
                },
                metadata={
                    "error_kind": "permission_denied",
                    "permission_phase": "runtime",
                    "permission_source": runtime_permission.source,
                    "permission_mode": runtime_permission.mode,
                },
                lifecycle={
                    **lifecycle,
                    "permission_decision": {
                        "phase": "runtime",
                        "allowed": False,
                        "source": runtime_permission.source,
                        "mode": runtime_permission.mode,
                        "reason": runtime_permission.reason,
                    },
                    "progress_events": progress_events,
                },
            )

        permission = await prepared_call.tool.check_permission(prepared_call.parsed_input, context)
        if not permission.allowed:
            self._emit_hook_event(
                event_name="PermissionDenied",
                context=context,
                tool_name=request.name,
                payload={"reason": permission.reason, "source": permission.source or "tool", "mode": getattr(permission, "mode", "deny")},
            )
            return self._finalize_error_record(
                tool_call_id=tool_call_id,
                request=request,
                status=AuditToolCallStatus.DENIED.value,
                is_concurrency_safe=prepared_call.is_concurrency_safe,
                started=started,
                message=permission.reason or "Tool permission denied",
                output_payload={
                    "permission_source": permission.source or "tool",
                    "permission_mode": getattr(permission, "mode", "deny"),
                    "permission_reason": permission.reason,
                    "guardrail_code": getattr(permission, "guardrail_code", None),
                },
                metadata={
                    "error_kind": "permission_denied",
                    "permission_phase": "tool",
                    "permission_source": permission.source or "tool",
                    "permission_mode": getattr(permission, "mode", "deny"),
                },
                lifecycle={
                    **lifecycle,
                    "permission_decision": {
                        "phase": "tool",
                        "allowed": False,
                        "source": permission.source or "tool",
                        "mode": getattr(permission, "mode", "deny"),
                        "reason": permission.reason,
                    },
                    "progress_events": progress_events,
                },
            )

        self._emit_hook_event(event_name="PreToolUse", context=context, tool_name=request.name)
        context.report_progress(event="tool_start", message=f"Starting {prepared_call.tool.user_facing_name(prepared_call.parsed_input)}")
        try:
            timeout_seconds = self._resolve_tool_timeout(prepared_call.tool, prepared_call.parsed_input, context)
            execute_coro = prepared_call.tool.execute(prepared_call.parsed_input, context)
            result = (
                await asyncio.wait_for(execute_coro, timeout=timeout_seconds)
                if timeout_seconds is not None and timeout_seconds > 0
                else await execute_coro
            )
        except asyncio.TimeoutError:
            timeout_seconds = self._resolve_tool_timeout(prepared_call.tool, prepared_call.parsed_input, context)
            message = RUNTIME_TOOL_TIMEOUT_HINT
            self._emit_hook_event(
                event_name="PostToolUseFailure",
                context=context,
                tool_name=request.name,
                payload={"error": message, "error_kind": "timeout_error", "timeout_seconds": timeout_seconds},
            )
            return self._finalize_error_record(
                tool_call_id=tool_call_id,
                request=request,
                status=AuditToolCallStatus.FAILED.value,
                is_concurrency_safe=prepared_call.is_concurrency_safe,
                started=started,
                message=message,
                output_payload={"error_kind": "timeout_error", "timeout_seconds": timeout_seconds},
                metadata={"error_kind": "timeout_error", "timeout_seconds": timeout_seconds},
                lifecycle={**lifecycle, "progress_events": progress_events},
            )
        except asyncio.CancelledError as exc:
            self._emit_hook_event(
                event_name="PostToolUseFailure",
                context=context,
                tool_name=request.name,
                payload={"error": str(exc), "error_kind": "interrupted"},
            )
            return self._finalize_error_record(
                tool_call_id=tool_call_id,
                request=request,
                status=AuditToolCallStatus.FAILED.value,
                is_concurrency_safe=prepared_call.is_concurrency_safe,
                started=started,
                message="Tool execution interrupted",
                metadata={"error_kind": "interrupted"},
                lifecycle={**lifecycle, "progress_events": progress_events},
            )
        except Exception as exc:
            error_kind = _classify_execution_error_kind(exc)
            self._emit_hook_event(
                event_name="PostToolUseFailure",
                context=context,
                tool_name=request.name,
                payload={"error": str(exc), "error_kind": error_kind},
            )
            return self._finalize_error_record(
                tool_call_id=tool_call_id,
                request=request,
                status=AuditToolCallStatus.FAILED.value,
                is_concurrency_safe=prepared_call.is_concurrency_safe,
                started=started,
                message=str(exc),
                metadata={"error_kind": error_kind},
                lifecycle={**lifecycle, "progress_events": progress_events},
            )

        self._emit_hook_event(event_name="PostToolUse", context=context, tool_name=request.name)
        context.report_progress(event="tool_complete", message=f"Completed {prepared_call.tool.user_facing_name(prepared_call.parsed_input)}")
        duration_ms = max(0, int((perf_counter() - started) * 1000))
        output_payload = dict(result.output_payload or {})
        if result.context_modifier is not None:
            output_payload.setdefault("context_modifier", dict(result.context_modifier))
        self._session_store.complete_tool_call(
            tool_call_id,
            status=AuditToolCallStatus.COMPLETED.value,
            output_payload=output_payload,
            error_message=None,
            duration_ms=duration_ms,
        )
        result.output_payload = output_payload
        result.metadata = {
            **dict(result.metadata or {}),
            "progress_event_count": len(progress_events),
        }
        lifecycle.update(
            {
                "permission_decision": {
                    "phase": "tool",
                    "allowed": True,
                    "source": permission.source or runtime_permission.source or "runtime",
                    "mode": getattr(permission, "mode", "allow"),
                    "reason": permission.reason,
                },
                "progress_events": progress_events,
            }
        )
        if result.context_modifier is not None:
            lifecycle["context_modifier"] = dict(result.context_modifier)
        return ToolExecutionRecord(
            tool_call_id=tool_call_id,
            request=request,
            status=AuditToolCallStatus.COMPLETED.value,
            is_concurrency_safe=prepared_call.is_concurrency_safe,
            result=result,
            error_message=None,
            duration_ms=duration_ms,
            lifecycle=lifecycle,
        )

    def _record_progress_event(
        self,
        *,
        session_id: str,
        turn_id: str,
        tool_use_id: str,
        tool_name: str,
        progress_events: list[dict[str, Any]],
        payload: dict[str, Any],
        progress_callback: Callable[[ToolExecutionUpdate], None] | None = None,
    ) -> None:
        event_payload = {
            "kind": "runtime_tool_progress",
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            **dict(payload or {}),
        }
        progress_events.append(dict(event_payload))
        self._session_store.create_checkpoint(
            session_id=session_id,
            turn_id=turn_id,
            checkpoint_type=AuditCheckpointType.AUTO,
            state_payload=event_payload,
        )
        if progress_callback is not None:
            progress_callback(
                ToolExecutionUpdate(
                    kind="progress",
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    progress_payload=dict(event_payload),
                )
            )

    def _emit_hook_event(self, *, event_name: str, context: ToolExecutionContext, tool_name: str, payload: dict[str, Any] | None = None) -> None:
        runtime_state = self._session_store.load_runtime_state(context.session_id)
        session_hooks = runtime_state.metadata.get("session_hooks") or {}
        payload_dict = dict(payload or {})
        persist_without_hook = (
            event_name == "PermissionDenied"
            and payload_dict.get("source") == "permission_rule"
        )
        wrote_checkpoint = False
        for skill_ref, hook_config in session_hooks.items():
            matched_hooks = match_runtime_event_hooks(hook_config, event_name=event_name, tool_name=tool_name)
            if not matched_hooks:
                continue
            self._session_store.create_checkpoint(
                session_id=context.session_id,
                turn_id=context.turn_id,
                checkpoint_type=AuditCheckpointType.AUTO,
                state_payload={
                    "kind": "runtime_hook",
                    "event": event_name,
                    "tool_name": tool_name,
                    "agent_type": self._agent_type,
                    "skill_ref": skill_ref,
                    "matched_hooks": matched_hooks,
                    **payload_dict,
                },
            )
            wrote_checkpoint = True
        if persist_without_hook and not wrote_checkpoint:
            self._session_store.create_checkpoint(
                session_id=context.session_id,
                turn_id=context.turn_id,
                checkpoint_type=AuditCheckpointType.AUTO,
                state_payload={
                    "kind": "runtime_hook",
                    "event": event_name,
                    "tool_name": tool_name,
                    "agent_type": self._agent_type,
                    "skill_ref": None,
                    "matched_hooks": [],
                    **payload_dict,
                },
            )

    def _finalize_error_record(
        self,
        *,
        tool_call_id: str,
        request: ToolCallRequest,
        status: str,
        is_concurrency_safe: bool,
        started: float,
        message: str,
        output_payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        lifecycle: dict[str, Any] | None = None,
    ) -> ToolExecutionRecord:
        duration_ms = max(0, int((perf_counter() - started) * 1000))
        result = ToolExecutionPayload(
            content=message,
            output_payload=dict(output_payload or {}),
            metadata=dict(metadata or {}),
            is_error=True,
        )
        self._session_store.complete_tool_call(
            tool_call_id,
            status=status,
            output_payload=result.output_payload,
            error_message=message,
            duration_ms=duration_ms,
        )
        return ToolExecutionRecord(
            tool_call_id=tool_call_id,
            request=request,
            status=status,
            is_concurrency_safe=is_concurrency_safe,
            result=result,
            error_message=message,
            duration_ms=duration_ms,
            lifecycle=dict(lifecycle or {}),
        )

    def _resolve_tool_timeout(
        self,
        tool: RuntimeTool,
        parsed_input: Any,
        context: ToolExecutionContext,
    ) -> float | None:
        try:
            timeout_seconds = tool.execution_timeout_seconds(parsed_input, context)
        except Exception:
            timeout_seconds = None
        if timeout_seconds is None:
            timeout_seconds = self._default_tool_timeout_seconds
        if timeout_seconds is None:
            return None
        try:
            resolved = float(timeout_seconds)
        except (TypeError, ValueError):
            return None
        return resolved if resolved > 0 else None


@dataclass(slots=True)
class _TrackedStreamingTool:
    prepared_call: _PreparedToolCall
    batch_id: int
    status: Literal["queued", "executing", "completed", "yielded"] = "queued"
    task: asyncio.Task[ToolExecutionRecord] | None = None
    pending_progress: list[ToolExecutionUpdate] = field(default_factory=list)
    record: ToolExecutionRecord | None = None


class StreamingToolExecutor:
    _SHELL_TOOL_NAMES = {"Bash", "PowerShell"}

    def __init__(
        self,
        *,
        orchestrator: ToolOrchestrator,
        session_id: str,
        turn_id: str,
        prepared_calls: list[_PreparedToolCall],
        session: Any,
        recon_payload: dict[str, Any],
        initial_context: dict[str, Any],
    ):
        self._orchestrator = orchestrator
        self._session_id = session_id
        self._turn_id = turn_id
        self._session = session
        self._recon_payload = dict(recon_payload or {})
        self._current_context = dict(initial_context or {})
        self._event = asyncio.Event()
        self._discarded = False
        self._shell_error = False
        self._shell_error_message = ""
        self._batch_context_modifiers: dict[int, list[dict[str, Any]]] = {}
        self._tracked_tools: list[_TrackedStreamingTool] = []
        batch_id = 0
        for is_concurrency_safe, batch in orchestrator._partition_batches(prepared_calls):
            for prepared_call in batch:
                self._tracked_tools.append(
                    _TrackedStreamingTool(
                        prepared_call=prepared_call,
                        batch_id=batch_id,
                    )
                )
            batch_id += 1

    def add_tool_call_request(self, request: ToolCallRequest) -> None:
        prepared_call = self._orchestrator._prepare_call(request)
        self.add_prepared_call(prepared_call)

    def add_prepared_call(self, prepared_call: _PreparedToolCall) -> None:
        batch_id = self._next_batch_id_for(prepared_call)
        self._tracked_tools.append(_TrackedStreamingTool(prepared_call=prepared_call, batch_id=batch_id))
        self._event.set()

    async def process_queue(self) -> None:
        await self._start_ready_tools()

    def get_completed_updates(self) -> list[ToolExecutionUpdate]:
        return self._drain_updates()

    def discard(self) -> None:
        self._discarded = True
        for tracked in self._tracked_tools:
            if tracked.status == "executing" and tracked.task is not None:
                tracked.task.cancel()
        self._event.set()

    def get_updated_context(self) -> dict[str, Any]:
        return dict(self._current_context)

    async def get_remaining_updates(self) -> AsyncGenerator[ToolExecutionUpdate, None]:
        try:
            while self._has_unfinished_tools():
                await self._start_ready_tools()

                yielded = False
                for update in self._drain_updates():
                    yielded = True
                    yield update

                if not yielded and self._has_unfinished_tools():
                    await self._event.wait()
                    self._event.clear()

            for update in self._drain_updates():
                yield update
        except asyncio.CancelledError:
            self.discard()
            running = [
                tracked.task
                for tracked in self._tracked_tools
                if tracked.task is not None and not tracked.task.done()
            ]
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            raise

    async def _start_ready_tools(self) -> None:
        for tracked in self._tracked_tools:
            if tracked.status != "queued":
                continue
            if self._discarded:
                await self._complete_with_synthetic_error(tracked, "Streaming fallback - tool execution discarded")
                continue
            if self._shell_error:
                await self._complete_with_synthetic_error(
                    tracked,
                    self._shell_error_message or "Cancelled: parallel shell tool call errored",
                )
                continue
            if self._can_execute_tool(tracked):
                self._start_tool(tracked)
            elif not tracked.prepared_call.is_concurrency_safe:
                break

    def _can_execute_tool(self, tracked: _TrackedStreamingTool) -> bool:
        for item in self._tracked_tools:
            if item.batch_id >= tracked.batch_id:
                break
            if item.status != "yielded":
                return False
        executing = [item for item in self._tracked_tools if item.status == "executing"]
        if not executing:
            return True
        return tracked.prepared_call.is_concurrency_safe and all(
            item.prepared_call.is_concurrency_safe for item in executing
        )

    def _next_batch_id_for(self, prepared_call: _PreparedToolCall) -> int:
        if not self._tracked_tools:
            return 0
        last = self._tracked_tools[-1]
        if not prepared_call.is_concurrency_safe:
            return last.batch_id + 1
        if not last.prepared_call.is_concurrency_safe:
            return last.batch_id + 1
        if prepared_call.concurrency_key:
            current_batch_keys = {
                tracked.prepared_call.concurrency_key
                for tracked in self._tracked_tools
                if tracked.batch_id == last.batch_id and tracked.prepared_call.concurrency_key
            }
            if prepared_call.concurrency_key in current_batch_keys:
                return last.batch_id + 1
        return last.batch_id

    def _start_tool(self, tracked: _TrackedStreamingTool) -> None:
        tracked.status = "executing"

        def on_progress(update: ToolExecutionUpdate) -> None:
            tracked.pending_progress.append(update)
            self._event.set()

        async def runner() -> None:
            record = await self._orchestrator._execute_prepared_call(
                tracked.prepared_call,
                session_id=self._session_id,
                turn_id=self._turn_id,
                session=self._session,
                recon_payload=self._recon_payload,
                progress_callback=on_progress,
            )
            tracked.record = record
            tracked.status = "completed"
            if record.result.context_modifier:
                self._batch_context_modifiers.setdefault(tracked.batch_id, []).append(dict(record.result.context_modifier))
            if record.result.is_error and self._is_shell_tool(tracked):
                self._shell_error = True
                self._shell_error_message = f"Cancelled: parallel shell tool call {tracked.prepared_call.request.name} errored"
                for sibling in self._tracked_tools:
                    if sibling is tracked:
                        continue
                    if sibling.status == "executing" and sibling.task is not None:
                        sibling.task.cancel()
            self._event.set()

        tracked.task = asyncio.create_task(runner())

    async def _complete_with_synthetic_error(self, tracked: _TrackedStreamingTool, message: str) -> None:
        tracked.record = await self._orchestrator._execute_prepared_call(
            tracked.prepared_call,
            session_id=self._session_id,
            turn_id=self._turn_id,
            session=self._session,
            recon_payload=self._recon_payload,
            skip_execution_reason=message,
        )
        tracked.status = "completed"
        self._event.set()

    def _drain_updates(self) -> list[ToolExecutionUpdate]:
        updates: list[ToolExecutionUpdate] = []
        yielded_batch_ids: set[int] = set()
        for index, tracked in enumerate(self._tracked_tools):
            while tracked.pending_progress:
                updates.append(tracked.pending_progress.pop(0))

            if tracked.status == "yielded":
                continue
            if tracked.status == "completed" and tracked.record is not None:
                prior_in_batch = [
                    item
                    for item in self._tracked_tools[:index]
                    if item.batch_id == tracked.batch_id and item.status != "yielded"
                ]
                if prior_in_batch:
                    continue
                tracked.status = "yielded"
                updates.append(
                    ToolExecutionUpdate(
                        kind="record",
                        tool_use_id=tracked.prepared_call.request.id,
                        tool_name=tracked.prepared_call.request.name,
                        record=tracked.record,
                        new_context=dict(self._current_context),
                    )
                )
                yielded_batch_ids.add(tracked.batch_id)
                continue
            if tracked.status == "executing" and not tracked.prepared_call.is_concurrency_safe:
                break
        for batch_id in sorted(yielded_batch_ids):
            if not self._batch_is_fully_yielded(batch_id):
                continue
            modifiers = self._batch_context_modifiers.pop(batch_id, [])
            if not modifiers:
                continue
            for modifier in modifiers:
                self._current_context = _merge_tool_context(self._current_context, modifier)
            updates.append(ToolExecutionUpdate(kind="context", new_context=dict(self._current_context)))
        return updates

    def _batch_is_fully_yielded(self, batch_id: int) -> bool:
        batch_tools = [tracked for tracked in self._tracked_tools if tracked.batch_id == batch_id]
        return bool(batch_tools) and all(tracked.status == "yielded" for tracked in batch_tools)

    def _has_unfinished_tools(self) -> bool:
        return any(tracked.status != "yielded" for tracked in self._tracked_tools)

    @classmethod
    def _is_shell_tool(cls, tracked: _TrackedStreamingTool) -> bool:
        tool = tracked.prepared_call.tool
        return bool(tool is not None and tool.name in cls._SHELL_TOOL_NAMES)


def _merge_tool_context(current_context: dict[str, Any], modifier: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current_context or {})
    for key, value in dict(modifier or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**dict(merged.get(key) or {}), **dict(value)}
        else:
            merged[key] = value
    return merged
