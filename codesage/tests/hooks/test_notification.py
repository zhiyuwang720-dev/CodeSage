"""阶段 09 §2.5 通知 emit 测试(§9.1 test_notification.py):四源触发 + 字段正确 +
fail-open + 审计红线 + statusbar 消费(有/无头)。

引擎层假件:RecordingHooks 记录 notify 调用(事件分发零路径,§4.10.1),不真
spawn 钩子;分发本身的行为由 test_manager.py 的 notify 用例覆盖。
"""

import asyncio
import io
import os
import shutil

from codesage.ai import LLMError, StreamEvent
from codesage.cli.repl import _render_notification
from codesage.cli import statusbar as sb_mod
from codesage.cli.statusbar import StatusBar
from codesage.engine import AgentLoop, AgentLoopConfig
from codesage.permissions import PermissionEngine
from codesage.permissions.audit import JsonlAuditSink
from codesage.tools import Tool, ToolError, ToolRegistry, ToolResult


class FakeLLM:
    """Scripted stream; a raising entry propagates (LLMError → provider error 路径)。"""

    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.total_cost = [0.0]

    def stream(self, request, model="main"):
        return self._gen()

    async def _gen(self):
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for ev in self.script[idx](self.calls):
            await asyncio.sleep(0)
            yield ev


class RecordingHooks:
    """记录 notify 调用的 HookManager 假件;其余事件零路径(§4.10.1)。"""

    def __init__(self, fail_notify=False):
        self.events = []  # [(notification_type, message, title, data)]
        self.fail_notify = fail_notify

    def has_hooks_for_event(self, event):
        return False  # 仅 notify 路径(engine 其余事件零路径,不干扰)

    async def notify(self, notification_type, message, *, title=None, **data):
        if self.fail_notify:
            raise RuntimeError("notify bug")
        self.events.append((notification_type, message, title, data))


def tool_use_event(name, tid, input_json):
    return [
        StreamEvent(type="tool_use_start", tool_use_id=tid, tool_name=name),
        StreamEvent(type="tool_use_delta", input_json_delta=input_json),
        StreamEvent(type="done", stop_reason="tool_use"),
    ]


def text_event(text="answer"):
    return [StreamEvent(type="text_delta", text=text), StreamEvent(type="done", stop_reason="end_turn")]


class PermTool(Tool):
    """需权限工具:引擎默认 ask(§5.2 同款)。"""

    name = "Perm"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return True

    async def _run(self, input, ctx):
        return ToolResult(f"perm ok:{input['text']}")


class ErrTool(Tool):
    """ToolError 工具:触发 tool_error 通知源。"""

    name = "Err"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        raise ToolError("boom", code="boom_code")


def _loop(llm, hooks, **kw):
    registry = ToolRegistry([PermTool(), ErrTool()])
    permissions = kw.pop("permissions", None) or PermissionEngine()
    return AgentLoop(
        AgentLoopConfig(
            client=llm, tools=registry, permissions=permissions, hooks=hooks, **kw
        )
    )


async def _collect(loop, user_input="hi"):
    return [m async for m in loop.run(user_input)]


# ---------------------------------------------------------------------------
# 四源 emit(字段正确)


async def test_permission_request_emitted(tmp_path):
    """permission_request:进入 request_permission 前 emit,字段含 tool/input/mode/reason。"""
    from codesage.core import Session

    async def approve(decision, tool, input):
        return True

    hooks = RecordingHooks()
    llm = FakeLLM([lambda i: tool_use_event("Perm", "t1", '{"text": "x"}'), lambda i: text_event()])
    await _collect(_loop(llm, hooks, request_permission=approve, session=Session("s1", tmp_path)))

    assert [e[0] for e in hooks.events] == ["permission_request"]
    _, message, title, data = hooks.events[0]
    assert message == "Permission requested: Perm"
    assert title == "Perm"
    assert data["tool_name"] == "Perm"
    assert data["tool_input"] == {"text": "x"}
    assert data["mode"] == "ask"
    assert data["session_id"] != ""
    assert data["cwd"] != ""


async def test_permission_denied_emitted():
    """permission_denied:ask 被拒 → 先 request 后 denied,reason 可追溯。"""
    async def decline(decision, tool, input):
        return False

    hooks = RecordingHooks()
    llm = FakeLLM([lambda i: tool_use_event("Perm", "t1", '{"text": "x"}'), lambda i: text_event()])
    messages = await _collect(_loop(llm, hooks, request_permission=decline))

    assert messages[2].content[0].is_error
    assert [e[0] for e in hooks.events] == ["permission_request", "permission_denied"]
    _, message, title, data = hooks.events[1]
    assert message == "Permission denied: Perm"
    assert title == "Perm"
    assert data["reason"] is not None


async def test_permission_denied_without_request_permission():
    """单发模式(request_permission=None):无询问 → 只 emit permission_denied。"""
    hooks = RecordingHooks()
    llm = FakeLLM([lambda i: tool_use_event("Perm", "t1", '{"text": "x"}'), lambda i: text_event()])
    messages = await _collect(_loop(llm, hooks))

    assert messages[2].content[0].is_error
    assert [e[0] for e in hooks.events] == ["permission_denied"]


async def test_tool_error_emitted():
    """tool_error:ToolError catch → 通知含 tool_name + error_code;错误路径不打断主循环。"""
    hooks = RecordingHooks()
    llm = FakeLLM([lambda i: tool_use_event("Err", "t1", "{}"), lambda i: text_event()])
    messages = await _collect(_loop(llm, hooks))

    assert messages[2].content[0].is_error
    assert "boom" in str(messages[2].content[0].content)
    assert llm.calls == 2  # 模型看到错误后自愈继续
    assert [e[0] for e in hooks.events] == ["tool_error"]
    _, message, title, data = hooks.events[0]
    assert message == "Tool error: Err: boom"
    assert data["tool_name"] == "Err"
    assert data["error_code"] == "boom_code"


async def test_llm_error_emitted():
    """llm_error:LLMError → provider error 消息位,status_code 进 data。"""
    def raise_error(i):
        raise LLMError("HTTP 500: provider down", status_code=500)

    hooks = RecordingHooks()
    llm = FakeLLM([raise_error])
    messages = await _collect(_loop(llm, hooks))

    assert messages[-1].is_error
    assert "(provider error" in str(messages[-1].content)
    assert [e[0] for e in hooks.events] == ["llm_error"]
    _, message, title, data = hooks.events[0]
    assert message == "LLM error: HTTP 500: provider down"
    assert data["status_code"] == 500


# ---------------------------------------------------------------------------
# fail-open(§2.5):通知失败不影响权限询问/错误路径


async def test_notify_fail_open_does_not_break_permission_flow():
    """notify 抛错仅日志:权限弹窗照常,工具执行成功。"""
    async def approve(decision, tool, input):
        return True

    hooks = RecordingHooks(fail_notify=True)
    llm = FakeLLM([lambda i: tool_use_event("Perm", "t1", '{"text": "x"}'), lambda i: text_event()])
    messages = await _collect(_loop(llm, hooks, request_permission=approve))

    assert messages[2].content[0].content == "perm ok:x"
    assert not messages[2].content[0].is_error


async def test_notify_fail_open_does_not_break_error_path():
    """错误路径 + notify 抛错:工具错误照常进消息流,不抛给引擎。"""
    hooks = RecordingHooks(fail_notify=True)
    llm = FakeLLM([lambda i: tool_use_event("Err", "t1", "{}"), lambda i: text_event()])
    messages = await _collect(_loop(llm, hooks))

    assert messages[2].content[0].is_error
    assert "boom" in str(messages[2].content[0].content)


# ---------------------------------------------------------------------------
# 审计红线(§9.2):通知不产生权限审计事件


async def test_notification_no_extra_permission_audit(tmp_path):
    """「每决策恰好一条」:deny 流程恰一条权限审计事件,通知不新增(§9.2 红线)。"""
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    hooks = RecordingHooks()
    llm = FakeLLM([lambda i: tool_use_event("Perm", "t1", '{"text": "x"}'), lambda i: text_event()])
    loop = _loop(
        llm, hooks,
        permissions=PermissionEngine(audit_sink=sink),
    )
    await _collect(loop)

    events = list(sink.load())  # 权限审计流:仅引擎决策一条,通知不新增(§9.2 红线)
    assert len(events) == 1


# ---------------------------------------------------------------------------
# statusbar 消费(有/无头模式)


class FakeTTY(io.StringIO):
    """tty 形状流,使 StatusBar.enable() 生效(test_statusbar 同款)。"""

    def isatty(self):
        return True


async def test_on_notification_ui_callback_fires():
    """有头模式:loop.on_notification 收到 (type, message, data);与 hooks 分发并存。"""
    async def approve(decision, tool, input):
        return True

    seen = []
    hooks = RecordingHooks()
    llm = FakeLLM([lambda i: tool_use_event("Perm", "t1", '{"text": "x"}'), lambda i: text_event()])
    loop = _loop(llm, hooks, request_permission=approve)
    loop.on_notification = lambda ntype, message, data: seen.append((ntype, message, dict(data)))
    await _collect(loop)

    assert seen and seen[0][0] == "permission_request"
    assert "Permission requested" in seen[0][1]
    assert seen[0][2]["tool_name"] == "Perm"


async def test_headless_no_ui_callback_hooks_still_dispatched():
    """无头模式(--output-format json,不建 bar):on_notification 保持 None → 不渲染,
    通知仍进 hooks 分发(hooks.jsonl + 日志,§2.5)。"""
    hooks = RecordingHooks()
    llm = FakeLLM([lambda i: tool_use_event("Perm", "t1", '{"text": "x"}'), lambda i: text_event()])
    loop = _loop(llm, hooks)  # 不设 on_notification → 无 UI 通道
    await _collect(loop)

    assert [e[0] for e in hooks.events] == ["permission_denied"]


def test_notification_line_via_bar_print_below(monkeypatch):
    """消费端冒烟:启用 bar 时通知行经 print_below 落入滚动区;未启用(无头)→ 零输出。"""
    monkeypatch.setattr(sb_mod, "USE_COLOR", True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((80, 24)))
    out = FakeTTY()
    bar = StatusBar(model_name="m", out=out)
    bar.enable()
    out.seek(0)
    out.truncate()

    line = _render_notification("tool_error", "Tool error: Err: boom")
    bar.print_below(line)
    assert "[tool_error] Tool error: Err: boom" in out.getvalue()

    bar.disable()
    out.seek(0)
    out.truncate()
    bar.print_below(_render_notification("llm_error", "LLM error: down"))  # 未启用 → 无输出
    assert out.getvalue() == ""
