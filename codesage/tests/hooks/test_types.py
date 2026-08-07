"""契约层测试(§9.1 test_types.py):HookJSONOutput 校验矩阵 / HookInput 事件字段 / HookSpec 配置校验 / 通知枚举 / 审计事件。"""

import json

import pytest

from codesage.hooks import (
    EVENTS,
    NOTIFICATION_TYPES,
    HookAuditEvent,
    HookExecutor,
    HookInput,
    HookJSONOutput,
    HookManagerProtocol,
    HookResult,
    HookSpec,
    HookValidationError,
    is_notification_type,
)


# ---------------------------------------------------------------------------
# HookInput(§2.1/§2.2)


def test_hook_input_base_fields_flat_merge():
    """基础三字段(session_id/cwd/session_path)恒定存在,事件字段经 extra 扁平合并。"""
    inp = HookInput(
        session_id="s1",
        cwd="/work",
        session_path="/work/.codesage/session.jsonl",
        extra={"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "tu_1"},
    )
    data = inp.to_dict()
    assert data["session_id"] == "s1"
    assert data["cwd"] == "/work"
    assert data["session_path"] == "/work/.codesage/session.jsonl"
    assert data["tool_name"] == "Bash"
    assert data["tool_input"] == {"command": "ls"}
    assert data["tool_use_id"] == "tu_1"


def test_hook_input_extra_cannot_override_base_fields():
    """extra 键与基础三字段冲突时以基础字段为准(防御,事件字段命名不可重叠)。"""
    inp = HookInput(
        session_id="s1", cwd="/work", session_path="/p",
        extra={"session_id": "evil", "tool_name": "Bash"},
    )
    data = inp.to_dict()
    assert data["session_id"] == "s1"
    assert data["tool_name"] == "Bash"


def test_hook_input_event_specific_fields():
    """各事件独有字段(§2.2 事件表)经 extra 携带,to_json 可往返。"""
    cases = [
        ({"source": "resume", "model": "quick"}, None),
        ({"prompt": "你好"}, None),
        ({"tool_name": "Write", "tool_input": {"file_path": "/x"}, "tool_use_id": "tu"}, None),
        (
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "tu",
             "tool_response": {"content": "ok", "is_error": False}},
            None,
        ),
        ({"reason": "completed", "last_assistant_message": "done"}, None),
        (
            {"trigger": "auto", "context_tokens": 1234, "window": 8, "reserve": 1,
             "keep_recent": 3},
            None,
        ),
        ({"trigger": "auto", "compact_summary": "sum", "cut_index": 5}, None),
        ({"notification_type": "permission_request", "message": "m", "title": "t"}, None),
    ]
    for extra, _ in cases:
        inp = HookInput(session_id="s", cwd="/c", session_path="/p", extra=extra)
        assert json.loads(inp.to_json()) == {
            "session_id": "s", "cwd": "/c", "session_path": "/p", **extra
        }


# ---------------------------------------------------------------------------
# HookJSONOutput(§4.4 校验矩阵)


def test_parse_full_pre_tool_use_output():
    """PreToolUse 合法全字段:permissionDecision/updatedInput/immune 等。"""
    out, warnings = HookJSONOutput.parse(
        json.dumps(
            {
                "permissionDecision": "allow",
                "permissionDecisionReason": "guard passed",
                "updatedInput": {"command": "ls -la"},
                "immune": True,
                "systemMessage": "ok",
                "suppressOutput": True,
            }
        ),
        "PreToolUse",
    )
    assert warnings == []
    assert out.permissionDecision == "allow"
    assert out.permissionDecisionReason == "guard passed"
    assert out.updatedInput == {"command": "ls -la"}
    assert out.immune is True
    assert out.systemMessage == "ok"
    assert out.suppressOutput is True
    assert out.event == "PreToolUse"


def test_parse_user_prompt_submit_output():
    """UserPromptSubmit:updatedPrompt/updatedSystemReminder/additionalContext 三通道。"""
    out, warnings = HookJSONOutput.parse(
        json.dumps(
            {
                "updatedPrompt": "改写后的问题",
                "updatedSystemReminder": "reminder",
                "additionalContext": "ctx",
            }
        ),
        "UserPromptSubmit",
    )
    assert warnings == []
    assert out.updatedPrompt == "改写后的问题"
    assert out.updatedSystemReminder == "reminder"
    assert out.additionalContext == "ctx"


def test_parse_stop_output():
    """Stop:continue=false + stopReason(§6.4 continue:false 位)。"""
    out, _ = HookJSONOutput.parse(json.dumps({"continue": False, "stopReason": "完成"}), "Stop")
    assert out.continue_ is False
    assert out.stopReason == "完成"


def test_parse_unknown_field_rejected():
    """未知字段 → 校验失败拒绝整个输出(§4.4,CC 同款 fail-closed)。"""
    with pytest.raises(HookValidationError, match="unknown or event-mismatched field 'nope'"):
        HookJSONOutput.parse(json.dumps({"nope": 1}), "PreToolUse")


def test_parse_event_mismatched_field_rejected():
    """事件不匹配字段(Stop 带 permissionDecision)→ 校验失败(§4.4 事件名校验是安全位)。"""
    with pytest.raises(HookValidationError, match="permissionDecision"):
        HookJSONOutput.parse(json.dumps({"permissionDecision": "allow"}), "Stop")
    # decision 别名也仅 PreToolUse
    with pytest.raises(HookValidationError, match="decision"):
        HookJSONOutput.parse(json.dumps({"decision": "approve"}), "UserPromptSubmit")
    # updatedInput 仅 PreToolUse
    with pytest.raises(HookValidationError, match="updatedInput"):
        HookJSONOutput.parse(json.dumps({"updatedInput": {"x": 1}}), "PostToolUse")


def test_parse_async_field_rejected():
    """async/asyncTimeout 不在 v1 schema,出现即校验失败(§4.4)。"""
    with pytest.raises(HookValidationError, match="'async'"):
        HookJSONOutput.parse(json.dumps({"async": True}), "PreToolUse")
    with pytest.raises(HookValidationError, match="'asyncTimeout'"):
        HookJSONOutput.parse(json.dumps({"asyncTimeout": 30}), "PreToolUse")


def test_parse_wrong_type_rejected():
    """字段类型错误 → 校验失败。"""
    with pytest.raises(HookValidationError, match="must be str"):
        HookJSONOutput.parse(json.dumps({"updatedPrompt": 123}), "UserPromptSubmit")
    with pytest.raises(HookValidationError, match="must be dict"):
        HookJSONOutput.parse(json.dumps({"updatedInput": "not-a-dict"}), "PreToolUse")
    with pytest.raises(HookValidationError, match="must be bool"):
        HookJSONOutput.parse(json.dumps({"continue": "yes"}), "Stop")
    # 非 None 的保留字段拒绝
    with pytest.raises(HookValidationError, match="hookSpecificOutput"):
        HookJSONOutput.parse(json.dumps({"hookSpecificOutput": {"future": 1}}), "PostToolUse")


def test_parse_null_enum_values_ok():
    """枚举字段 null 视为未提供(与 _expect 语义一致),不触发枚举校验。"""
    out, _ = HookJSONOutput.parse(json.dumps({"permissionDecision": None}), "PreToolUse")
    assert out.permissionDecision is None
    out, _ = HookJSONOutput.parse(json.dumps({"decision": None}), "PreToolUse")
    assert out.decision is None


def test_parse_invalid_enums_rejected():
    """permissionDecision 枚举限 allow|deny(v1 无 ask,§5.2);decision 限 approve|block。"""
    with pytest.raises(HookValidationError, match="permissionDecision must be 'allow'\\|'deny'"):
        HookJSONOutput.parse(json.dumps({"permissionDecision": "ask"}), "PreToolUse")
    with pytest.raises(HookValidationError, match="decision must be 'approve'\\|'block'"):
        HookJSONOutput.parse(json.dumps({"decision": "grant"}), "PreToolUse")


def test_parse_non_json_rejected():
    """stdout 不以 `{` 开头由执行层判定 plainText(§4.3);此处非法 JSON / 非对象一律拒绝。"""
    with pytest.raises(HookValidationError, match="invalid JSON"):
        HookJSONOutput.parse("{not json", "PreToolUse")
    with pytest.raises(HookValidationError, match="must be a JSON object"):
        HookJSONOutput.parse('"just a string"', "PreToolUse")
    with pytest.raises(HookValidationError, match="must be a JSON object"):
        HookJSONOutput.parse("[1, 2]", "PreToolUse")


def test_parse_unknown_event_rejected():
    with pytest.raises(HookValidationError, match="unknown hook event 'Nope'"):
        HookJSONOutput.parse("{}", "Nope")


def test_immune_without_allow_ignored():
    """immune 无 allow 同结果 → 免疫位忽略 + validation 警告(§5.5 约束 1)。"""
    out, warnings = HookJSONOutput.parse(json.dumps({"immune": True}), "PreToolUse")
    assert out.immune is False
    assert len(warnings) == 1
    assert "immune: true ignored" in warnings[0]


def test_immune_with_allow_kept():
    """immune 仅与 permissionDecision=allow 同结果时生效(§5.5)。"""
    out, warnings = HookJSONOutput.parse(
        json.dumps({"permissionDecision": "allow", "immune": True}), "PreToolUse"
    )
    assert out.immune is True
    assert warnings == []
    out, _ = HookJSONOutput.parse(
        json.dumps({"permissionDecision": "deny", "immune": True}), "PreToolUse"
    )
    assert out.immune is False  # deny 与 immune 并存 → 免疫位忽略


def test_parse_empty_object_ok():
    """空输出 `{}` → 无决策的 passthrough(§5.2 无决策 → 引擎照常)。"""
    out, warnings = HookJSONOutput.parse("{}", "PreToolUse")
    assert warnings == []
    assert out.permissionDecision is None
    assert out.continue_ is False


# ---------------------------------------------------------------------------
# HookSpec(§3.1 配置校验)


def test_spec_command_defaults():
    """command 钩子:type/command 必填,timeout 默认 60(§4.2)。"""
    spec = HookSpec.from_dict({"type": "command", "command": "scripts/guard.sh"}, "PreToolUse")
    assert spec is not None
    assert spec.type == "command"
    assert spec.command == "scripts/guard.sh"
    assert spec.timeout == 60
    assert spec.if_ is None
    assert spec.if_evaluable is True


def test_spec_prompt_defaults():
    """prompt 钩子:timeout 默认 30,model 可选。"""
    spec = HookSpec.from_dict(
        {"type": "prompt", "prompt": "评估:$ARGUMENTS", "model": "quick"}, "PreToolUse"
    )
    assert spec is not None
    assert spec.timeout == 30
    assert spec.model == "quick"


def test_spec_http_full():
    """http 钩子:url/headers/allowedEnvVars,timeout 默认 60(§4.9)。"""
    spec = HookSpec.from_dict(
        {
            "type": "http",
            "url": "http://127.0.0.1:8000/guard",
            "headers": {"Authorization": "Bearer $TOKEN"},
            "allowedEnvVars": ["TOKEN"],
            "timeout": 10,
        },
        "PreToolUse",
    )
    assert spec is not None
    assert spec.url == "http://127.0.0.1:8000/guard"
    assert spec.headers == {"Authorization": "Bearer $TOKEN"}
    assert spec.allowedEnvVars == ["TOKEN"]
    assert spec.timeout == 10


def test_spec_required_payload_missing_rejected():
    """必填 payload 缺失(如 command 无 command)→ 丢弃 + warning。"""
    warnings: list[str] = []
    spec = HookSpec.from_dict({"type": "command"}, "PreToolUse", warn=warnings.append)
    assert spec is None
    assert any("non-empty 'command'" in w for w in warnings)


def test_spec_unknown_type_rejected():
    warnings: list[str] = []
    spec = HookSpec.from_dict({"type": "agent", "command": "x"}, "PreToolUse", warn=warnings.append)
    assert spec is None
    assert any("unknown hook type" in w for w in warnings)


def test_spec_unknown_field_rejected():
    """未知钩子字段 → 丢弃 + warning(§3.1)。"""
    warnings: list[str] = []
    spec = HookSpec.from_dict(
        {"type": "command", "command": "x", "typo": 1}, "PreToolUse", warn=warnings.append
    )
    assert spec is None
    assert any("unknown hook fields ['typo']" in w for w in warnings)


def test_spec_invalid_timeout_rejected():
    """非法 timeout(0/负数/字符串/bool)→ 丢弃 + warning(§3.1)。"""
    for bad in (0, -5, "10", True):
        warnings: list[str] = []
        spec = HookSpec.from_dict(
            {"type": "command", "command": "x", "timeout": bad}, "PreToolUse", warn=warnings.append
        )
        assert spec is None, f"timeout={bad!r} should be rejected"
        assert any("invalid timeout" in w for w in warnings)


def test_spec_if_on_evaluable_events():
    """PreToolUse/PostToolUse 可带 if(§2.4),if_evaluable=True。"""
    for event in ("PreToolUse", "PostToolUse"):
        spec = HookSpec.from_dict({"type": "command", "command": "x", "if": "Bash(git *)"}, event)
        assert spec is not None
        assert spec.if_ == "Bash(git *)"
        assert spec.if_evaluable is True


def test_spec_if_on_non_evaluable_event_warns():
    """非可求值事件(Stop)带 if → 解析期 warning + 永不执行,配置本身不丢弃(§2.4)。"""
    warnings: list[str] = []
    spec = HookSpec.from_dict(
        {"type": "command", "command": "x", "if": "Bash(git *)"}, "Stop", warn=warnings.append
    )
    assert spec is not None
    assert spec.if_evaluable is False
    assert any("cannot evaluate 'if'" in w for w in warnings)


def test_spec_field_type_restrictions():
    """model 仅 prompt;headers/allowedEnvVars 仅 http(§3.1)。"""
    warnings: list[str] = []
    assert HookSpec.from_dict(
        {"type": "command", "command": "x", "model": "quick"}, "PreToolUse", warn=warnings.append
    ) is None
    assert any("'model' only applies to prompt" in w for w in warnings)

    warnings = []
    assert HookSpec.from_dict(
        {"type": "command", "command": "x", "headers": {"A": "B"}}, "PreToolUse",
        warn=warnings.append,
    ) is None
    assert any("'headers' only applies to http" in w for w in warnings)

    warnings = []
    assert HookSpec.from_dict(
        {"type": "prompt", "prompt": "p", "allowedEnvVars": ["X"]}, "PreToolUse",
        warn=warnings.append,
    ) is None
    assert any("'allowedEnvVars' only applies to http" in w for w in warnings)


def test_spec_if_wrong_type_rejected():
    warnings: list[str] = []
    spec = HookSpec.from_dict(
        {"type": "command", "command": "x", "if": 123}, "PreToolUse", warn=warnings.append
    )
    assert spec is None
    assert any("'if' must be a string" in w for w in warnings)


def test_spec_headers_value_wrong_type_rejected():
    warnings: list[str] = []
    spec = HookSpec.from_dict(
        {"type": "http", "url": "http://127.0.0.1:8000/", "headers": {"A": 1}},
        "PreToolUse",
        warn=warnings.append,
    )
    assert spec is None
    assert any("'headers' must be a dict of str->str" in w for w in warnings)


def test_spec_allowed_env_vars_element_wrong_type_rejected():
    warnings: list[str] = []
    spec = HookSpec.from_dict(
        {"type": "http", "url": "http://127.0.0.1:8000/", "allowedEnvVars": ["TOKEN", 1]},
        "PreToolUse",
        warn=warnings.append,
    )
    assert spec is None
    assert any("'allowedEnvVars' must be a list of str" in w for w in warnings)


def test_spec_empty_payload_rejected():
    """payload 仅空白字符 → 丢弃(§3.1 必填 string)。"""
    for payload in ("", "   "):
        warnings: list[str] = []
        spec = HookSpec.from_dict(
            {"type": "command", "command": payload}, "PreToolUse", warn=warnings.append
        )
        assert spec is None
        assert any("non-empty 'command'" in w for w in warnings)


def test_spec_unknown_event_rejected():
    warnings: list[str] = []
    spec = HookSpec.from_dict({"type": "command", "command": "x"}, "Nope", warn=warnings.append)
    assert spec is None
    assert any("unknown hook event" in w for w in warnings)


def test_spec_non_object_entry_rejected():
    warnings: list[str] = []
    spec = HookSpec.from_dict("not-a-dict", "PreToolUse", warn=warnings.append)
    assert spec is None


# ---------------------------------------------------------------------------
# notification_type 枚举(§2.5)


def test_notification_type_enum():
    """四个通知源枚举值合法;其他值非法(§2.5 四值表)。"""
    assert NOTIFICATION_TYPES == (
        "permission_request",
        "permission_denied",
        "tool_error",
        "llm_error",
    )
    for value in NOTIFICATION_TYPES:
        assert is_notification_type(value)
    for bad in ("idle", "auth", "", "permission"):
        assert not is_notification_type(bad)


# ---------------------------------------------------------------------------
# HookAuditEvent(§8.1)


def test_audit_event_serialization():
    """HookAuditEvent asdict 序列化包含全部字段;timestamp 由 sink 盖章(对齐 ToolAuditEvent)。"""
    from dataclasses import asdict

    ev = HookAuditEvent(
        event="PreToolUse",
        hook_type="command",
        command="scripts/guard.sh",
        matched=True,
        outcome="success",
        exit_code=0,
        duration_ms=42,
        stderr_summary=None,
    )
    data = asdict(ev)
    assert data["event"] == "PreToolUse"
    assert data["hook_type"] == "command"
    assert data["matched"] is True
    assert data["outcome"] == "success"
    assert data["exit_code"] == 0
    assert data["duration_ms"] == 42
    assert data["timestamp"] == ""  # sink 盖章


def test_audit_event_accepts_notification_type_as_event():
    """HookAuditEvent.event 可扩展 notification_type 值(§2.5 审计)。"""
    ev = HookAuditEvent(
        event="permission_request",
        hook_type="prompt",
        command="评估",
        matched=True,
        outcome="success",
        exit_code=0,
        duration_ms=10,
        stderr_summary="",
    )
    assert ev.event == "permission_request"


# ---------------------------------------------------------------------------
# base.py 执行器/结果形状


def test_hook_result_defaults():
    result = HookResult(exit_code=0)
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.duration_ms == 0


def test_protocols_exist():
    """HookExecutor/HookManagerProtocol 是协议;具体实现(S3/S4/S10/S5)后续步骤落地。"""
    assert hasattr(HookExecutor, "run")
    assert hasattr(HookManagerProtocol, "dispatch")
    assert hasattr(HookManagerProtocol, "notify")
    assert hasattr(HookManagerProtocol, "has_hooks_for_event")


def test_events_constant():
    """八事件表(§2.2)。"""
    assert EVENTS == (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "PreCompact",
        "PostCompact",
        "Notification",
    )
