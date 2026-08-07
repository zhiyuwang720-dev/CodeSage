"""提示执行体测试(§9.1 test_prompt.py):prompt 模板 `$ARGUMENTS` 替换;`{ok,reason}`
契约(强制 JSON,含字段缺失/非法输出 → fail-closed);ok:false 按事件区分(PreToolUse →
deny 且 reason 进审计与拒绝文案 / Stop → 阻止停止 / UserPromptSubmit → 阻止提交);
JSON 解析失败/超时 → fail-closed(PreToolUse deny,Stop 放行+警告);ok:true → 无决策;
模型指针 quick + 失败回退 main;mock client 调用形状;assemble 冒烟。

执行体单元测试直接驱动 PromptHookExecutor;按事件语义经 load_hook_manager 装配的
HookManager 全管线验证(§4.10);quick 回退 main 用真实 LLMClient + httpx.MockTransport
(沿用 test_client.py 模式,不真发请求)。
"""

import asyncio
import json

import httpx
import pytest

from codesage.ai import ContentBlock, LLMClient, LLMError, LLMResponse, Message
from codesage.cli.assemble import build_loop
from codesage.config import GlobalConfig, paths
from codesage.hooks import HookInput, HookValidationError, load_hook_manager
from codesage.hooks.command import HookExecutionError
from codesage.hooks.prompt import PromptHookExecutor, SYSTEM_PROMPT, parse_hook_output
from codesage.permissions.audit import JsonlAuditSink
from codesage.tools import ToolRegistry, get_builtin_tools

INPUT = json.dumps({"session_id": "s1", "cwd": "C:/proj", "session_path": "C:/proj/s.jsonl"})


# ---------------------------------------------------------------------------
# 测试基建:记录调用形状的假 client + 全管线装配


class FakeLLM:
    """脚本化假 client:complete 记录 (model, LLMRequest) 调用形状,按脚本返回/抛错。"""

    def __init__(self, responses=None, errors=None):
        self.responses = list(responses or [])
        self.errors = list(errors or [])
        self.calls = []  # [(model, LLMRequest)]
        self.total_cost = [0.0]

    async def complete(self, request, model="main"):
        self.calls.append((model, request))
        if self.errors:
            raise self.errors.pop(0)
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(content=[ContentBlock(type="text", text='{"ok": true}')])


class SlowLLM:
    """挂起假 client:超时测试用,永远不返回。"""

    async def complete(self, request, model="main"):
        await asyncio.sleep(10)
        return LLMResponse()


def text_response(text):
    return LLMResponse(content=[ContentBlock(type="text", text=text)])


def base_input(extra=None):
    return HookInput(
        session_id="s1",
        cwd="/work",
        session_path="/work/session.jsonl",
        extra=extra,
    )


def tool_input_extra(tool_name, tool_input=None):
    return {"tool_name": tool_name, "tool_input": tool_input or {}, "tool_use_id": "tu1"}


def build_prompt_manager(cfg, client, *, tmp_path):
    """装配冒烟侧:load_hook_manager(§3.2 装配入口)+ 真实 JsonlAuditSink 双流。"""
    return load_hook_manager(
        cfg,
        client=client,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        hooks_sink=JsonlAuditSink(tmp_path / "hooks.jsonl"),
        registry=ToolRegistry(get_builtin_tools()),
    )


# ---------------------------------------------------------------------------
# 执行体单元:$ARGUMENTS 替换 / mock client 调用形状 / ok:true / ok:false


async def test_arguments_replacement_all_occurrences():
    """§4.1/§4.10.4:`$ARGUMENTS` 全部出现替换为 HookInput JSON(最小版,无索引)。"""
    client = FakeLLM()
    ex = PromptHookExecutor("评估: $ARGUMENTS \n再次: $ARGUMENTS", client=client)
    r = await ex.run(INPUT, timeout=5)
    assert r.exit_code == 0
    model, request = client.calls[0]
    assert request.messages == [Message(role="user", content=f"评估: {INPUT} \n再次: {INPUT}")]


async def test_mock_client_call_shape():
    """mock client 调用形状:单 user 消息 + system 提示 + model 缺省 "quick" 指针。"""
    client = FakeLLM()
    ex = PromptHookExecutor("safe? $ARGUMENTS", client=client)
    await ex.run(INPUT, timeout=5)
    model, request = client.calls[0]
    assert model == "quick"  # §3.1:model 缺省 "quick" 指针
    assert request.system == SYSTEM_PROMPT
    assert len(request.messages) == 1
    assert request.messages[0].role == "user"
    assert request.messages[0].content == "safe? " + INPUT
    assert request.tools is None  # §4.7:prompt 钩子无工具能力


async def test_model_passthrough():
    """§3.1:显式 model 透传(不默认 quick)。"""
    client = FakeLLM()
    ex = PromptHookExecutor("x", client=client, model="compact")
    await ex.run(INPUT, timeout=5)
    assert client.calls[0][0] == "compact"


async def test_ok_true_no_decision_result():
    """§4.8:ok:true → exit 0 + 空 stdout(S5 走 plainText 分支,无决策)。"""
    client = FakeLLM(responses=[text_response('{"ok": true}')])
    r = await PromptHookExecutor("x", client=client).run(INPUT, timeout=5)
    assert r.exit_code == 0
    assert r.stdout == ""  # {ok,...} 不进 stdout(会被 S5 当 HookJSONOutput 误解析)


async def test_ok_false_blocks_with_reason():
    """§4.8:ok:false → exit 2 + stderr=reason(阻塞信号,消费动作同 exit 2 行)。"""
    client = FakeLLM(responses=[text_response(json.dumps({"ok": False, "reason": "unsafe"}))])
    r = await PromptHookExecutor("x", client=client).run(INPUT, timeout=5)
    assert r.exit_code == 2
    assert r.stderr == "unsafe"


async def test_ok_false_without_reason():
    """§4.8:ok:false 无 reason(CC schema 中 reason optional)→ 空文案,deny 仍生效。"""
    client = FakeLLM(responses=[text_response('{"ok": false}')])
    r = await PromptHookExecutor("x", client=client).run(INPUT, timeout=5)
    assert r.exit_code == 2
    assert r.stderr == ""


# ---------------------------------------------------------------------------
# {ok,reason} 契约解析矩阵(§4.8:字段缺失/非法输出 → fail-closed)


@pytest.mark.parametrize(
    "text",
    [
        "",  # 空响应
        "not json at all",  # 非 JSON
        "[1, 2]",  # 非对象
        '{"ok": "true"}',  # ok 非 bool
        '{"ok": 1}',  # ok 非 bool
        '{"reason": "why"}',  # ok 缺失
        '{"ok": true, "extra": 1}',  # 未知字段(additionalProperties: false)
        '{"ok": true, "reason": 42}',  # reason 非 str
    ],
)
def test_parse_hook_output_invalid(text):
    """§4.8:字段缺失/非法输出 → HookValidationError(fail-closed 依据)。"""
    with pytest.raises(HookValidationError):
        parse_hook_output(text)


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"ok": true}', (True, None)),
        ('{"ok": true, "reason": "fine"}', (True, "fine")),
        ('{"ok": false}', (False, None)),
        ('{"ok": false, "reason": "no"}', (False, "no")),
        ('  {"ok": true}  ', (True, None)),  # 前后空白宽容
    ],
)
def test_parse_hook_output_valid(text, expected):
    assert parse_hook_output(text) == expected


async def test_empty_model_response_fail_closed():
    """§4.8:模型返回空文本 → 输出不可解析 → HookValidationError。"""
    client = FakeLLM(responses=[LLMResponse()])
    with pytest.raises(HookValidationError):
        await PromptHookExecutor("x", client=client).run(INPUT, timeout=5)


# ---------------------------------------------------------------------------
# 失败路径:LLM 调用失败 / is_error 响应 / 无 client / 超时(fail-closed 依据)


async def test_llm_error_fail_closed():
    """§4.6:LLM 调用失败(回退 main 后仍失败)→ HookExecutionError(spawn 失败同档)。"""
    client = FakeLLM(errors=[LLMError("provider down", status_code=500)])
    with pytest.raises(HookExecutionError, match="LLM call failed"):
        await PromptHookExecutor("x", client=client).run(INPUT, timeout=5)


async def test_is_error_response_fail_closed():
    """§4.6:错误被既有链路渲染为响应(is_error)→ 同 LLM 调用失败。"""
    client = FakeLLM(responses=[LLMResponse(is_error=True, error_message="bad key")])
    with pytest.raises(HookExecutionError, match="bad key"):
        await PromptHookExecutor("x", client=client).run(INPUT, timeout=5)


async def test_no_client_fail_closed():
    """§4.6:client 未装配(None)→ fail-closed,不静默放行。"""
    ex = PromptHookExecutor("x", client=None)
    with pytest.raises(HookExecutionError, match="LLM client"):
        await ex.run(INPUT, timeout=5)


async def test_timeout_raises():
    """§4.2/§4.6:LLM 调用挂起超时 → TimeoutError(fail-closed 依据,不等挂起结束)。"""
    ex = PromptHookExecutor("x", client=SlowLLM())
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError, match="timed out"):
        await ex.run(INPUT, timeout=0.2)
    assert asyncio.get_running_loop().time() - started < 5


# ---------------------------------------------------------------------------
# 全管线(load_hook_manager 装配,§4.10):ok:false 按事件区分 / fail-closed / 无决策


async def test_ok_false_pre_tool_use_deny_with_reason_in_audit(tmp_path):
    """§4.8:PreToolUse ok:false → deny;reason 进拒绝文案与双流审计。"""
    client = FakeLLM(responses=[text_response('{"ok": false, "reason": "unsafe"}')])
    mgr = build_prompt_manager(
        {"PreToolUse": [{"hooks": [{"type": "prompt", "prompt": "guard $ARGUMENTS"}]}]},
        client,
        tmp_path=tmp_path,
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash", {"command": "rm"})))
    assert r.permission_decision == "deny"
    assert r.hook_allowed is False
    assert r.deny_reason == "Permission denied by hook guard $ARGUMENTS: unsafe"
    # 权限流(audit.jsonl):reason 进审计,source=hook:PreToolUse(§8.1)
    audit = JsonlAuditSink(tmp_path / "audit.jsonl").load()
    assert len(audit) == 1
    assert audit[0]["decision"] == "deny" and "unsafe" in audit[0]["reason"]
    assert audit[0]["source"] == "hook:PreToolUse"
    # 执行流(hooks.jsonl):恰好一条,outcome=blocked(exit 2 同判,§8.1)
    hooks = JsonlAuditSink(tmp_path / "hooks.jsonl").load()
    assert len(hooks) == 1
    assert hooks[0]["hook_type"] == "prompt"
    assert hooks[0]["outcome"] == "blocked"
    assert hooks[0]["exit_code"] == 2
    assert hooks[0]["stderr_summary"] == "unsafe"


async def test_ok_false_stop_blocks_stop(tmp_path):
    """§4.8:Stop ok:false → 阻止停止(reason 注入 feedback 继续循环,§6.4 exit 2 同路径)。"""
    client = FakeLLM(responses=[text_response('{"ok": false, "reason": "not done"}')])
    mgr = build_prompt_manager(
        {"Stop": [{"hooks": [{"type": "prompt", "prompt": "done?"}]}]},
        client,
        tmp_path=tmp_path,
    )
    r = await mgr.dispatch("Stop", input=base_input({"reason": "completed"}))
    assert r.stop_feedback == "not done"  # 阻止停止
    assert r.stop is False  # prompt 无 continue:false 通道(§4.10.6 exit 2 行)


async def test_ok_false_user_prompt_blocks_submit(tmp_path):
    """§4.8:UserPromptSubmit ok:false → 阻止提交(输入丢弃,§4.3 exit 2 同路径)。"""
    client = FakeLLM(responses=[text_response('{"ok": false, "reason": "blocked input"}')])
    mgr = build_prompt_manager(
        {"UserPromptSubmit": [{"hooks": [{"type": "prompt", "prompt": "allow?"}]}]},
        client,
        tmp_path=tmp_path,
    )
    r = await mgr.dispatch("UserPromptSubmit", input=base_input({"prompt": "do a bad thing"}))
    assert r.blocking_error == "blocked input"


async def test_ok_true_no_decision(tmp_path):
    """§4.8:ok:true → 无决策:引擎照常;无权限审计事件(§5.2 表)。"""
    client = FakeLLM(responses=[text_response('{"ok": true}')])
    mgr = build_prompt_manager(
        {"PreToolUse": [{"hooks": [{"type": "prompt", "prompt": "guard"}]}]},
        client,
        tmp_path=tmp_path,
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash", {"command": "ls"})))
    assert r.permission_decision is None
    assert r.hook_allowed is False
    assert JsonlAuditSink(tmp_path / "audit.jsonl").load() == []  # 无决策不审计(§8.1)


async def test_parse_failure_pre_tool_use_deny(tmp_path):
    """§4.6/§4.8:JSON 解析失败 → PreToolUse deny(输出不可解析即无法证明安全)。"""
    client = FakeLLM(responses=[text_response("not json at all")])
    mgr = build_prompt_manager(
        {"PreToolUse": [{"hooks": [{"type": "prompt", "prompt": "guard"}]}]},
        client,
        tmp_path=tmp_path,
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash", {"command": "rm"})))
    assert r.permission_decision == "deny"
    assert "not valid JSON" in r.deny_reason
    hooks = JsonlAuditSink(tmp_path / "hooks.jsonl").load()
    assert hooks[0]["outcome"] == "validation_error"


async def test_parse_failure_stop_fail_open(tmp_path):
    """§4.6/§4.8:JSON 解析失败 → Stop 放行 + warning(挂起不得把对话困住)。"""
    client = FakeLLM(responses=[text_response("not json at all")])
    mgr = build_prompt_manager(
        {"Stop": [{"hooks": [{"type": "prompt", "prompt": "done?"}]}]},
        client,
        tmp_path=tmp_path,
    )
    r = await mgr.dispatch("Stop", input=base_input({"reason": "completed"}))
    assert r.stop_feedback is None  # 放行
    assert r.stop is False
    assert r.permission_decision is None
    hooks = JsonlAuditSink(tmp_path / "hooks.jsonl").load()
    assert hooks[0]["outcome"] == "validation_error"


async def test_timeout_pre_tool_use_deny(tmp_path):
    """§4.2/§4.6:超时 → PreToolUse deny(fail-closed;Stop 放行路径同上 test 放行)。"""
    mgr = build_prompt_manager(
        {"PreToolUse": [{"hooks": [{"type": "prompt", "prompt": "slow", "timeout": 1}]}]},
        SlowLLM(),
        tmp_path=tmp_path,
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash", {"command": "rm"})))
    assert r.permission_decision == "deny"
    assert "timed out" in r.deny_reason
    hooks = JsonlAuditSink(tmp_path / "hooks.jsonl").load()
    assert hooks[0]["outcome"] == "timeout"


async def test_load_hook_manager_wires_model_field(tmp_path):
    """§3.1:配置的 model 字段经 load_hook_manager 工厂透传给执行体。"""
    client = FakeLLM()
    mgr = build_prompt_manager(
        {"PreToolUse": [{"hooks": [{"type": "prompt", "prompt": "guard", "model": "compact"}]}]},
        client,
        tmp_path=tmp_path,
    )
    await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash", {"command": "ls"})))
    assert client.calls[0][0] == "compact"


# ---------------------------------------------------------------------------
# 模型指针 quick + 失败回退 main(既有机制,真实 LLMClient + MockTransport,不真发请求)


def _cfg(tmp_path, monkeypatch):
    """GlobalConfig:quick 指针 → fast profile(沿用 test_client.py 模式)。"""
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    cfg = GlobalConfig.load()
    cfg.model_profiles = {
        "main": {"provider": "openai_compatible", "model": "deepseek-chat", "base_url": "https://api.deepseek.com"},
        "fast": {"provider": "openai_compatible", "model": "qwen-plus", "base_url": "https://dashscope.example.com"},
    }
    cfg.model_pointers = {"main": "main", "task": "fast", "compact": "fast", "quick": "fast"}
    cfg.save()


def _ok_response(text='{"ok": true}'):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "model": "deepseek-v4-flash",
        },
    )


async def test_quick_failure_falls_back_to_main(tmp_path, monkeypatch):
    """§4.8:quick 指针失败自动回退 main(LLMClient 既有辅助请求回退);ok:true 无决策。"""
    _cfg(tmp_path, monkeypatch)
    request_bodies = []

    def handler(req):
        body = json.loads(req.content)
        request_bodies.append(body["model"])
        if body["model"] == "qwen-plus":
            return httpx.Response(401, text="bad key")
        return _ok_response()

    client = LLMClient(
        project_dir=str(tmp_path),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    ex = PromptHookExecutor("safe? $ARGUMENTS", client=client)  # model 缺省 quick
    r = await ex.run(INPUT, timeout=5)
    assert r.exit_code == 0  # ok:true → 无决策
    assert request_bodies == ["qwen-plus", "deepseek-chat"]  # 回退 main 后成功
    await client.aclose()


# ---------------------------------------------------------------------------
# assemble 冒烟(cli/assemble.py 装配:load_hook_manager + hooks.jsonl 接线)


async def test_assemble_build_loop_wires_hooks(tmp_path, monkeypatch):
    """§10 S10:build_loop 装配 hooks(快照解析);无配置事件走索引零路径。"""
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / ".codesage")
    (tmp_path / ".codesage").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".codesage" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "prompt", "prompt": "guard $ARGUMENTS"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    loop = build_loop(cwd=tmp_path)
    assert loop.hooks is not None
    assert loop.hooks.has_hooks_for_event("PreToolUse")
    assert not loop.hooks.has_hooks_for_event("Stop")  # 未配置事件零路径(§4.10.1)


async def test_assemble_build_loop_no_hooks(tmp_path, monkeypatch):
    """§10 S10:无 hooks 配置 → HookManager 装配但全事件零路径,常规路径零侵入。"""
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / ".codesage")
    loop = build_loop(cwd=tmp_path)
    assert loop.hooks is not None
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop", "Notification"):
        assert not loop.hooks.has_hooks_for_event(event)
