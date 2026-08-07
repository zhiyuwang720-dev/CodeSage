"""Hooks contract layer (phase 09): input/output types, spec validation, audit event.

Contract layer only — config parsing (S2), executors (S3/S4/S10) and the
manager (S5) live elsewhere. Per spec these types are deliberately independent
of the AI message contract (ai/types.py): hooks speak JSON, not ContentBlocks.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("codesage.hooks")

# 事件表(§2.2):八个事件
EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "PreCompact",
    "PostCompact",
    "Notification",
)

# 通知类型枚举(§2.5):四个通知源
NOTIFICATION_TYPES = ("permission_request", "permission_denied", "tool_error", "llm_error")

# 执行体类型(§3.1)
HOOK_TYPES = ("command", "prompt", "http")

# 默认超时(§4.2):command/http 60s,prompt 30s;通知事件整体 10s 由 notify 覆盖
DEFAULT_TIMEOUTS = {"command": 60, "prompt": 30, "http": 60}

# if 条件仅 PreToolUse/PostToolUse 可求值(§2.4);其余事件带 if → warning + 永不执行
IF_EVALUABLE_EVENTS = ("PreToolUse", "PostToolUse")

# matcher 不生效事件(§2.3):带 matcher 也不生效,配置解析时警告
MATCHER_IGNORED_EVENTS = ("UserPromptSubmit", "Stop")

# HookAuditEvent.outcome 取值(§8.1)
HOOK_OUTCOMES = (
    "success",
    "blocked",  # exit 2(阻塞错误)
    "non_blocking_error",
    "timeout",
    "validation_error",
    "cancelled",  # abort 跳过
    "skipped",  # deny 短路后不再执行
)


def is_notification_type(value: str) -> bool:
    """notification_type 枚举校验(§2.5)。"""
    return value in NOTIFICATION_TYPES


# ---------------------------------------------------------------------------
# HookInput(§2.1/§2.2)


@dataclass(slots=True)
class HookInput:
    """Hook 输入:基础三字段(§2.1)+ 事件独有字段(§2.2,统一放 extra)。"""

    session_id: str
    cwd: str
    session_path: str
    extra: dict[str, Any] | None = None  # 事件独有字段(tool_name/prompt/notification_type/...)

    def to_dict(self) -> dict[str, Any]:
        """扁平化为传给钩子的 JSON 对象(§4.10.4:基础字段 + 事件字段)。"""
        data = {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "session_path": self.session_path,
        }
        if self.extra:
            # 基础三字段优先,extra 键冲突时忽略(防御性,事件字段命名与基础字段不可重叠)
            data.update({k: v for k, v in self.extra.items() if k not in data})
        return data

    def to_json(self) -> str:
        """惰性序列化的复用对象由 HookManager 持有(§4.10.4);此处仅便捷方法。"""
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# HookJSONOutput(§4.4)

# 各事件允许的字段集:事件不匹配字段(如 Stop 带 permissionDecision)→ 校验失败(§4.4,安全位)。
# async/asyncTimeout 不在 v1 schema,出现即校验失败(不进任何事件集合)。
_COMMON_OUTPUT_FIELDS = {"systemMessage", "suppressOutput", "hookSpecificOutput"}
_EVENT_OUTPUT_FIELDS: dict[str, set[str]] = {
    "SessionStart": _COMMON_OUTPUT_FIELDS | {"additionalContext"},
    "UserPromptSubmit": _COMMON_OUTPUT_FIELDS
    | {"updatedPrompt", "updatedSystemReminder", "additionalContext"},
    "PreToolUse": _COMMON_OUTPUT_FIELDS
    | {"decision", "permissionDecision", "permissionDecisionReason", "updatedInput", "immune"},
    "PostToolUse": _COMMON_OUTPUT_FIELDS,
    "Stop": _COMMON_OUTPUT_FIELDS | {"continue", "stopReason"},
    "PreCompact": _COMMON_OUTPUT_FIELDS,
    "PostCompact": _COMMON_OUTPUT_FIELDS,
    "Notification": _COMMON_OUTPUT_FIELDS,
}

# 校验失败时附带的期望 schema(§4.10.5:钩子作者不用查文档)
SCHEMA_HINT = "\n".join(
    f"  {event}: {', '.join(sorted(fields))}" for event, fields in _EVENT_OUTPUT_FIELDS.items()
)


class HookValidationError(ValueError):
    """JSON 输出解析/校验失败(§4.6 fail-closed 依据:PreToolUse → deny)。"""


def _expect(data: dict[str, Any], key: str, type_: type, event: str) -> None:
    """字段存在且非 None 时校验类型;None 视为未提供。"""
    if key in data and data[key] is not None and not isinstance(data[key], type_):
        raise HookValidationError(
            f"field {key!r} for event {event} must be {type_.__name__}, "
            f"got {type(data[key]).__name__}"
        )


@dataclass(slots=True)
class HookJSONOutput:
    """Hook JSON 输出(§4.4)。

    字段名镜像契约 JSON key(camelCase);`continue` 是 Python 关键字,存为 continue_。
    """

    event: str
    continue_: bool = False  # Stop: false → 停止,stopReason 作为停止原因
    stopReason: str | None = None
    decision: str | None = None  # 兼容别名 approve|block → allow|deny,仅 PreToolUse 有意义
    systemMessage: str | None = None
    suppressOutput: bool = False  # 接受但惰性(plainText 本就不落 transcript)
    permissionDecision: str | None = None  # allow|deny(v1 无 ask,§5.2)
    permissionDecisionReason: str | None = None
    updatedInput: dict[str, Any] | None = None  # 改写后的工具输入(§5.4)
    updatedPrompt: str | None = None  # UserPromptSubmit:替换提交的 prompt 文本
    updatedSystemReminder: str | None = None  # 下一次请求的一次性 reminder 前缀(§7.2)
    additionalContext: str | None = None  # SessionStart/UserPromptSubmit:一次性 reminder 段(§7.1)
    immune: bool = False  # safetyCheck bypass 免疫位(§5.5)
    hookSpecificOutput: Any = None  # v1 恒 null,保留字段

    @classmethod
    def parse(cls, raw: str, event: str) -> tuple["HookJSONOutput", list[str]]:
        """解析并校验 stdout JSON(§4.4)。返回 (output, warnings);失败抛 HookValidationError。"""
        if event not in EVENTS:
            raise HookValidationError(f"unknown hook event {event!r}; expected one of {EVENTS}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HookValidationError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise HookValidationError(
                f"hook output must be a JSON object, got {type(data).__name__}"
            )
        allowed = _EVENT_OUTPUT_FIELDS[event]
        for key in data:
            if key not in allowed:
                raise HookValidationError(
                    f"unknown or event-mismatched field {key!r} for event {event}; "
                    f"allowed fields:\n{SCHEMA_HINT}"
                )

        # 类型逐项校验(字段名/类型)
        _expect(data, "systemMessage", str, event)
        _expect(data, "stopReason", str, event)
        _expect(data, "permissionDecisionReason", str, event)
        _expect(data, "updatedPrompt", str, event)
        _expect(data, "updatedSystemReminder", str, event)
        _expect(data, "additionalContext", str, event)
        _expect(data, "updatedInput", dict, event)
        for key in ("continue", "suppressOutput", "immune"):
            _expect(data, key, bool, event)

        if "hookSpecificOutput" in data and data["hookSpecificOutput"] is not None:
            raise HookValidationError("hookSpecificOutput must be null in v1 (reserved field)")
        # null 视为未提供(与 _expect 同语义),仅在非 None 时校验枚举
        if data.get("decision") is not None and data["decision"] not in ("approve", "block"):
            raise HookValidationError(
                f"decision must be 'approve'|'block', got {data['decision']!r}"
            )
        if (
            data.get("permissionDecision") is not None
            and data["permissionDecision"] not in ("allow", "deny")
        ):
            raise HookValidationError(
                f"permissionDecision must be 'allow'|'deny', got {data['permissionDecision']!r}"
            )

        # immune 无 allow 同结果 → 免疫位忽略 + validation 警告(§5.5 约束 1)
        warnings: list[str] = []
        immune = bool(data.get("immune", False))
        if immune and data.get("permissionDecision") != "allow":
            warnings.append(
                "immune: true ignored: no permissionDecision=allow in the same result (§5.5)"
            )
            immune = False

        return cls(
            event=event,
            continue_=bool(data.get("continue", False)),
            stopReason=data.get("stopReason"),
            decision=data.get("decision"),
            systemMessage=data.get("systemMessage"),
            suppressOutput=bool(data.get("suppressOutput", False)),
            permissionDecision=data.get("permissionDecision"),
            permissionDecisionReason=data.get("permissionDecisionReason"),
            updatedInput=data.get("updatedInput"),
            updatedPrompt=data.get("updatedPrompt"),
            updatedSystemReminder=data.get("updatedSystemReminder"),
            additionalContext=data.get("additionalContext"),
            immune=immune,
            hookSpecificOutput=data.get("hookSpecificOutput"),
        ), warnings


# ---------------------------------------------------------------------------
# HookSpec(§3.1)


# 单钩子配置的合法字段(其余 → 丢弃 + warning,§3.1)
_SPEC_FIELDS = {
    "type",
    "command",
    "prompt",
    "url",
    "timeout",
    "if",
    "model",
    "headers",
    "allowedEnvVars",
}


@dataclass(slots=True)
class HookSpec:
    """单个钩子的配置表示(§3.1)。

    校验规则:未知字段 / 未知 type / 缺失必填 payload / 非法 timeout → 丢弃(返回 None)+ warning;
    非可求值事件带 `if` → warning + if_evaluable=False(永不执行,§2.4)。matcher 是组级字段,
    归属 S2 的组结构,不在单钩子配置内。
    """

    type: str  # command | prompt | http
    event: str  # 所属事件(解析时填入)
    command: str | None = None  # type=command 必填
    prompt: str | None = None  # type=prompt 必填
    url: str | None = None  # type=http 必填
    timeout: int = 60  # §4.2 默认:command/http 60,prompt 30;正数
    if_: str | None = None  # hook 级二级过滤(§2.4),权限规则语法 "Tool(content)"
    model: str | None = None  # 仅 prompt;默认 "quick" 指针,失败自动回退 main
    headers: dict[str, str] | None = None  # 仅 http;值 $VAR 插值仅限 allowedEnvVars(§4.9)
    allowedEnvVars: list[str] | None = None  # 仅 http;header 插值白名单
    if_evaluable: bool = True  # 非 PreToolUse/PostToolUse 带 if → False(永不执行)

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        event: str,
        *,
        warn: Callable[[str], None] | None = None,
    ) -> "HookSpec | None":
        """从 settings 配置 dict 构建单钩子;非法条目返回 None 并告警(§3.1)。"""
        logger.debug("parsing hook spec: %s", data)

        def _warn(msg: str) -> None:
            logger.warning(msg)
            if warn is not None:
                warn(msg)

        if event not in EVENTS:
            _warn(f"unknown hook event {event!r}: entry discarded")
            return None
        if not isinstance(data, dict):
            _warn(f"hook entry must be an object, got {type(data).__name__}: discarded")
            return None

        unknown = set(data) - _SPEC_FIELDS
        if unknown:
            _warn(f"unknown hook fields {sorted(unknown)}: entry discarded")
            return None

        hook_type = data.get("type")
        if hook_type not in HOOK_TYPES:
            _warn(f"unknown hook type {hook_type!r}: entry discarded")
            return None

        payload_key = {"command": "command", "prompt": "prompt", "http": "url"}[hook_type]
        payload = data.get(payload_key)
        if not isinstance(payload, str) or not payload.strip():
            _warn(f"hook of type {hook_type!r} requires a non-empty {payload_key!r} string: discarded")
            return None

        timeout = DEFAULT_TIMEOUTS[hook_type]
        if "timeout" in data:
            t = data["timeout"]
            # bool 是 int 子类,显式排除(True 不是合法秒数)
            if isinstance(t, bool) or not isinstance(t, int) or t <= 0:
                _warn(f"invalid timeout {t!r}: entry discarded")
                return None
            timeout = t

        if_ = data.get("if")
        if if_ is not None and not isinstance(if_, str):
            _warn(f"'if' must be a string, got {type(if_).__name__}: entry discarded")
            return None
        if_evaluable = event in IF_EVALUABLE_EVENTS
        if if_ is not None and not if_evaluable:
            # §2.4:非可求值事件带 if → 解析期 warning + 永不执行(不丢弃配置本身)
            _warn(
                f"event {event} cannot evaluate 'if' conditions (§2.4): "
                f"hook {payload!r} will never run"
            )

        model = data.get("model")
        if model is not None:
            if hook_type != "prompt":
                _warn(f"field 'model' only applies to prompt hooks: entry discarded")
                return None
            if not isinstance(model, str):
                _warn(f"'model' must be a string, got {type(model).__name__}: entry discarded")
                return None

        headers = data.get("headers")
        if headers is not None:
            if hook_type != "http":
                _warn(f"field 'headers' only applies to http hooks: entry discarded")
                return None
            if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
            ):
                _warn(f"'headers' must be a dict of str->str: entry discarded")
                return None

        allowed = data.get("allowedEnvVars")
        if allowed is not None:
            if hook_type != "http":
                _warn(f"field 'allowedEnvVars' only applies to http hooks: entry discarded")
                return None
            if not isinstance(allowed, list) or not all(isinstance(v, str) for v in allowed):
                _warn(f"'allowedEnvVars' must be a list of str: entry discarded")
                return None

        return cls(
            type=hook_type,
            event=event,
            command=data.get("command"),
            prompt=data.get("prompt"),
            url=data.get("url"),
            timeout=timeout,
            if_=if_,
            model=model,
            headers=headers,
            allowedEnvVars=allowed,
            if_evaluable=if_evaluable,
        )


# ---------------------------------------------------------------------------
# HookAuditEvent(§8.1)


@dataclass(slots=True)
class HookAuditEvent:
    """一次钩子调用恰好一条(§8.1 执行流 hooks.jsonl;追加写 + fsync 由 sink 负责)。"""

    event: str  # 八个事件之一,或 notification_type 值(permission_request 等)
    hook_type: str  # command | prompt | http
    command: str | None  # 命令/prompt 摘要(截断 200 字符)
    matched: bool
    outcome: str  # HOOK_OUTCOMES 之一
    exit_code: int | None
    duration_ms: int
    stderr_summary: str | None  # 前 200 字符
    timestamp: str = ""  # 由 sink 自动盖章(对齐 ToolAuditEvent 模式)
