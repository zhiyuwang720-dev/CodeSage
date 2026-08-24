"""core/session/src —— 会话内核实现。

导出清单照 DSH index.ts 的公共面:核心(Session/SessionStore/
install)、事件工具(invariant/surface/chunk_rows/request_header/
repair)、无损 JSON、类型注册表。包根 __init__.py 转发这里。
"""

from . import chunk_rows, invariant, json, known_event_types, preparation, repair, request_header, surface, types, typert
from .chunk_rows import decode_storage_record, pack_chunk_runs
from .index import (
    Session,
    SessionForkError,
    SessionForkSource,
    SessionStore,
    adopt_session_event,
    assert_adapter_defaults,
    assert_current_llm_shape,
    assert_message_event_shape,
    assert_session_event_envelope,
    assert_supported_request_header,
    collect_session_callbacks,
    has_provider_model,
    install,
    invoke_contained_session_observers,
    snapshot_session_event,
    snapshot_session_header,
    validate_restored_session_header,
    validate_session_header,
)
from .invariant import (
    SessionTrace,
    SessionTraceTransition,
    apply_transition,
    fresh_trace,
    require_open_step,
    seed_trace,
    validate_event,
)
from .json import FrozenDict, FrozenList, is_json_value, snapshot_json_value
from .known_event_types import KNOWN_SESSION_EVENT_TYPES, extend_event_types
from .preparation import SessionPreparation
from .repair import TOOL_NOT_STARTED, TOOL_OUTCOME_UNKNOWN, interrupted_turn_closers
from .request_header import canonical_header, fold_request_header, header_equals
from .surface import (
    SurfaceFoldReplacement,
    SurfaceFoldResult,
    SurfaceManager,
    SessionSurface,
    SurfacePlan,
    derive_event_message,
    fold_surface,
    is_append_surface_event,
    is_replacement_surface_event,
    is_surface_eligible_type,
    is_surface_event,
)
from .types import (
    REQUEST_HEADER_REASONS,
    SESSION_FORMAT_VERSION,
    SURFACE_EVENT_TYPES,
    TODO_STATUSES,
    TURN_END_REASON_KINDS,
    SessionId,
    is_surface_eligible_type,
)

__all__ = [
    # 核心
    "Session",
    "SessionStore",
    "SessionForkError",
    "SessionForkSource",
    "install",
    # 事件接纳边界
    "adopt_session_event",
    "snapshot_session_event",
    "validate_session_header",
    "validate_restored_session_header",
    "snapshot_session_header",
    "assert_session_event_envelope",
    "assert_current_llm_shape",
    "assert_adapter_defaults",
    "assert_message_event_shape",
    "has_provider_model",
    "assert_supported_request_header",
    "collect_session_callbacks",
    "invoke_contained_session_observers",
    # 不变式与表面
    "SessionTrace",
    "SessionTraceTransition",
    "apply_transition",
    "fresh_trace",
    "require_open_step",
    "seed_trace",
    "validate_event",
    "SurfaceFoldReplacement",
    "SurfaceFoldResult",
    "SurfaceManager",
    "SessionSurface",
    "SurfacePlan",
    "derive_event_message",
    "fold_surface",
    "is_append_surface_event",
    "is_replacement_surface_event",
    "is_surface_eligible_type",
    "is_surface_event",
    # 事件工具
    "interrupted_turn_closers",
    "TOOL_NOT_STARTED",
    "TOOL_OUTCOME_UNKNOWN",
    "pack_chunk_runs",
    "decode_storage_record",
    "canonical_header",
    "fold_request_header",
    "header_equals",
    "KNOWN_SESSION_EVENT_TYPES",
    "extend_event_types",
    # 无损 JSON
    "FrozenDict",
    "FrozenList",
    "is_json_value",
    "snapshot_json_value",
    # 类型词汇
    "SESSION_FORMAT_VERSION",
    "SURFACE_EVENT_TYPES",
    "TODO_STATUSES",
    "TURN_END_REASON_KINDS",
    "REQUEST_HEADER_REASONS",
    "SessionId",
    # 会话准备与注册表
    "SessionPreparation",
]
