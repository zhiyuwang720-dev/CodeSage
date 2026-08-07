"""HookManager 执行引擎测试(§9.1 test_manager.py):决策合并矩阵(§5.2)/执行流水线
(§4.10 短路、去重、if 过滤、聚合传递链)/双流审计(§8.1)/notify(§2.5)/abort(§6.3)。

mock 执行体注入:不真 spawn 子进程,用脚本驱动的 FakeExecutor(§9.1「mock 执行体注入」)。
"""

import asyncio
import json

import pytest

from codesage.hooks import (
    DEFAULT_TIMEOUTS,
    HookAuditEvent,
    HookInput,
    HookSpec,
    HookValidationError,
)
from codesage.hooks._common import HookGroup, parse_hook_config
from codesage.hooks.base import HookResult
from codesage.hooks.command import HookExecutionError
from codesage.hooks.registry import NOTIFICATION_TIMEOUT, HookManager
from codesage.permissions.audit import JsonlAuditSink, NullAuditSink
from codesage.tools import ToolRegistry, get_builtin_tools


# ---------------------------------------------------------------------------
# 测试基建:脚本驱动假执行体 + 管理器构造


class FakeExecutor:
    """脚本驱动假执行体:每次 run 弹出下一个行为(HookResult 或异常),不真 spawn。"""

    def __init__(self, name: str, script: list | None = None):
        self.name = name
        self.script = list(script or [])
        self.calls = 0
        self.inputs: list[str] = []
        self.timeouts: list[float] = []

    async def run(self, input_json: str, *, timeout: float) -> HookResult:
        self.calls += 1
        self.inputs.append(input_json)
        self.timeouts.append(timeout)
        if not self.script:
            return HookResult(exit_code=0)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def json_result(payload: dict, exit_code: int = 0, stderr: str = "") -> HookResult:
    """以 JSON 契约输出(HookJSONOutput,§4.4)构造 HookResult。"""
    return HookResult(exit_code=exit_code, stdout=json.dumps(payload), stderr=stderr)


def build_manager(
    cfg: dict,
    executors: dict[str, FakeExecutor],
    *,
    tmp_path=None,
    registry: ToolRegistry | None = None,
    http_hook_urls: list[str] | None = None,
) -> HookManager:
    """构造 HookManager:executor 工厂按 command/prompt/url 键查表注入假执行体。"""
    groups = parse_hook_config(cfg, http_hook_urls=http_hook_urls)
    assert groups, "test config must parse to at least one hook"

    def factory(spec: HookSpec):
        key = spec.command or spec.url or spec.prompt or ""
        ex = executors.get(key)
        assert ex is not None, f"no fake executor registered for {key!r}"
        return ex

    audit = JsonlAuditSink(tmp_path / "audit.jsonl") if tmp_path else NullAuditSink()
    hooks = JsonlAuditSink(tmp_path / "hooks.jsonl") if tmp_path else NullAuditSink()
    return HookManager(
        groups,
        executor_factory=factory,
        audit=audit,
        hooks_sink=hooks,
        registry=registry,
    )


def tool_input_extra(tool_name: str, tool_input: dict | None = None) -> dict:
    """PreToolUse 的 HookInput.extra(§2.2 事件表独有字段)。"""
    return {"tool_name": tool_name, "tool_input": tool_input or {}, "tool_use_id": "tu1"}


def base_input(extra: dict | None = None) -> HookInput:
    return HookInput(
        session_id="s1",
        cwd="/work",
        session_path="/work/session.jsonl",
        extra=extra,
    )


# ---------------------------------------------------------------------------
# 决策合并矩阵(§5.2)

async def test_deny_wins_over_allow():
    """deny 赢 allow:先 allow 后 deny → 终局 deny(§5.2 合并算法)。"""
    ex_allow = FakeExecutor("allow", [json_result({"permissionDecision": "allow"})])
    ex_deny = FakeExecutor("deny", [json_result({"permissionDecision": "deny", "permissionDecisionReason": "no"})])
    mgr = build_manager(
        {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "allow"}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "deny"}]},
            ]
        },
        {"allow": ex_allow, "deny": ex_deny},
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash", {"command": "rm -rf /"})))
    assert r.permission_decision == "deny"
    assert r.hook_allowed is False
    assert "Permission denied by hook deny" in r.deny_reason
    assert "no" in r.deny_reason
    assert r.deny_hook == "deny"
    assert ex_allow.calls == 1
    assert ex_deny.calls == 1


async def test_first_deny_short_circuits_rest(tmp_path):
    """首个 deny 短路:后续钩子不再执行,记 skipped(§5.2 合并算法/§8.1)。"""
    ex1 = FakeExecutor("one", [json_result({"permissionDecision": "deny", "permissionDecisionReason": "r1"})])
    ex2 = FakeExecutor("two", [json_result({"permissionDecision": "allow"})])
    ex3 = FakeExecutor("three", [json_result({"permissionDecision": "allow"})])
    mgr = build_manager(
        {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": c} for c in ("one", "two", "three")]}
            ]
        },
        {"one": ex1, "two": ex2, "three": ex3},
        tmp_path=tmp_path,
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert r.permission_decision == "deny"
    assert ex1.calls == 1 and ex2.calls == 0 and ex3.calls == 0
    events = json.loads((tmp_path / "hooks.jsonl").read_text(encoding="utf-8").splitlines()[-1].strip())
    outcomes = [json.loads(line)["outcome"] for line in (tmp_path / "hooks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert outcomes == ["success", "skipped", "skipped"]
    assert events["event"] == "PreToolUse"


async def test_allow_short_circuit_sets_hook_allowed():
    """allow 短路:无 deny 且任一 allow → hook_allowed=True(引擎不跑,§5.2 表)。"""
    ex = FakeExecutor("ok", [json_result({"permissionDecision": "allow", "permissionDecisionReason": "fine"})])
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "ok"}]}]},
        {"ok": ex},
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Write", {"file_path": "/src/a.py"})))
    assert r.permission_decision == "allow"
    assert r.hook_allowed is True
    assert r.deny_reason is None


async def test_no_decision_engine_runs():
    """无决策(全部 passthrough/无输出)→ permission_decision None,引擎照常(§5.2 表)。"""
    ex = FakeExecutor("quiet")
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "quiet"}]}]},
        {"quiet": ex},
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert r.permission_decision is None
    assert r.hook_allowed is False
    assert ex.calls == 1


async def test_updated_input_last_wins():
    """updatedInput last-wins:后钩子改写覆盖先钩子(§5.4)。"""
    ex1 = FakeExecutor("one", [json_result({"updatedInput": {"command": "safe"}})])
    ex2 = FakeExecutor("two", [json_result({"updatedInput": {"command": "safer"}})])
    mgr = build_manager(
        {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": "one"}, {"type": "command", "command": "two"}]}
            ]
        },
        {"one": ex1, "two": ex2},
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash", {"command": "rm"})))
    assert r.updated_input == {"command": "safer"}
    assert r.permission_decision is None  # 无决策,仅改写


async def test_passthrough_hook_can_rewrite_input():
    """无决策的 passthrough 钩子也可改写输入(§5.4,对齐 CC hookUpdatedInput 独立 yield)。"""
    ex = FakeExecutor("rewrite", [json_result({"updatedInput": {"file_path": "/src/other.py"}})])
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "rewrite"}]}]},
        {"rewrite": ex},
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Read", {"file_path": "/src/a.py"})))
    assert r.updated_input == {"file_path": "/src/other.py"}


async def test_deny_hook_updated_input_not_applied():
    """§5.2 守卫:deny 钩子的 updatedInput 不生效(不覆盖先前 allow 钩子的改写)。"""
    ex_allow = FakeExecutor("allow", [json_result({"permissionDecision": "allow", "updatedInput": {"command": "good"}})])
    ex_deny = FakeExecutor("deny", [json_result({"permissionDecision": "deny", "updatedInput": {"command": "evil"}})])
    mgr = build_manager(
        {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": "allow"}, {"type": "command", "command": "deny"}]}
            ]
        },
        {"allow": ex_allow, "deny": ex_deny},
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert r.permission_decision == "deny"
    # deny 钩子的改写被丢弃,保留 allow 钩子的改写(deny 终局,改写不生效,§5.2 守卫)
    assert r.updated_input == {"command": "good"}


async def test_immune_only_with_allow():
    """immune 仅与 permissionDecision=allow 同结果生效(§5.5);无 allow 的 immune 被忽略。"""
    ex1 = FakeExecutor("imm", [json_result({"permissionDecision": "allow", "immune": True})])
    mgr1 = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "imm"}]}]},
        {"imm": ex1},
    )
    r1 = await mgr1.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert r1.immune is True
    assert r1.hook_allowed is True

    # immune 无 allow 同结果 → types.parse 校验即忽略(§5.5 约束 1),manager 不设置
    from codesage.hooks import HookJSONOutput

    out, warnings = HookJSONOutput.parse(json.dumps({"immune": True}), "PreToolUse")
    assert out.immune is False
    assert any("immune: true ignored" in w for w in warnings)


async def test_decision_alias_approve_block():
    """§4.4 兼容别名:decision=approve→allow、block→deny(仅 PreToolUse 有意义)。"""
    ex_allow = FakeExecutor("app", [json_result({"decision": "approve"})])
    mgr_allow = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "app"}]}]},
        {"app": ex_allow},
    )
    r = await mgr_allow.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert r.permission_decision == "allow"

    ex_deny = FakeExecutor("blk", [json_result({"decision": "block"})])
    mgr_deny = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "blk"}]}]},
        {"blk": ex_deny},
    )
    r = await mgr_deny.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert r.permission_decision == "deny"


async def test_exit_2_denies_on_pretooluse():
    """PreToolUse exit 2 → deny,stderr 为 deny 原因(§4.3/§4.6)。"""
    ex = FakeExecutor("blocker", [HookResult(exit_code=2, stderr="rule X violated")])
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "blocker"}]}]},
        {"blocker": ex},
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert r.permission_decision == "deny"
    assert "rule X violated" in r.deny_reason


async def test_exit_1_fail_open_no_decision():
    """exit 1 → fail-open:非阻塞错误记录,流程继续(§4.6 表,显式自声明≠失败)。"""
    ex = FakeExecutor("grumpy", [HookResult(exit_code=1, stderr="i complain but allow")])
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "grumpy"}]}]},
        {"grumpy": ex},
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert r.permission_decision is None  # 不 deny:引擎照常求值


# ---------------------------------------------------------------------------
# fail-closed(§4.6):超时 / 校验失败 / spawn 失败 → PreToolUse deny,其他事件非阻塞

@pytest.mark.parametrize("exception", [TimeoutError("took too long")])
async def test_timeout_fail_closed_pretooluse(exception):
    ex = FakeExecutor("slow", [exception])
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "slow"}]}]},
        {"slow": ex},
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert r.permission_decision == "deny"
    assert "took too long" in r.deny_reason


async def test_execution_error_fail_closed_pretooluse():
    """spawn 失败(HookExecutionError)→ PreToolUse deny(§4.6 表第三行)。"""
    ex = FakeExecutor("missing", [HookExecutionError("failed to spawn hook process")])
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "missing"}]}]},
        {"missing": ex},
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert r.permission_decision == "deny"
    assert "failed to spawn" in r.deny_reason


async def test_validation_error_fail_closed_pretooluse():
    """JSON 校验失败 → PreToolUse deny(§4.6 第一行:输出不可解析即无法证明安全)。"""
    ex = FakeExecutor("bad", [HookResult(exit_code=0, stdout='{"permissionDecision": "allow", "bogus": 1}')])
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "bad"}]}]},
        {"bad": ex},
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert r.permission_decision == "deny"
    assert "bogus" in r.deny_reason  # 校验错误信息含期望 schema(§4.10.5)


async def test_failures_non_blocking_on_other_events(tmp_path):
    """非 PreToolUse 事件:超时/校验失败 → 非阻塞错误,不拖垮主循环(§4.6 表)。"""
    ex = FakeExecutor("broken", [TimeoutError("boom")])
    mgr = build_manager(
        {"PostToolUse": [{"hooks": [{"type": "command", "command": "broken"}]}]},
        {"broken": ex},
        tmp_path=tmp_path,
    )
    r = await mgr.dispatch("PostToolUse", input=base_input({"tool_name": "Bash"}))
    assert r.permission_decision is None  # 观察型事件无决策位
    outcome = json.loads((tmp_path / "hooks.jsonl").read_text(encoding="utf-8").splitlines()[0])["outcome"]
    assert outcome == "timeout"


async def test_stdout_truncation_fail_closed(tmp_path):
    """stdout 超 256KB → 截断;截断的 JSON 解析失败 → validation_error → PreToolUse deny
    (§4.10.5,与 test_command 的截断断言互补:此处验证 manager 的 fail-closed 消费)。"""
    big = "{" + "x" * 300_000
    ex = FakeExecutor("flood", [HookResult(exit_code=0, stdout=big)])
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "flood"}]}]},
        {"flood": ex},
        tmp_path=tmp_path,
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert r.permission_decision == "deny"
    outcome = json.loads((tmp_path / "hooks.jsonl").read_text(encoding="utf-8").splitlines()[0])["outcome"]
    assert outcome == "validation_error"


# ---------------------------------------------------------------------------
# 执行流水线(§4.10):短路索引 / 去重 / matcher / if / 惰性序列化

async def test_no_hooks_short_circuit_no_spawn(tmp_path):
    """无配置事件 → 索引空 → 零路径:不 spawn、不进管线(§4.10.1)。"""
    ex = FakeExecutor("never")
    mgr = build_manager(
        {"SessionStart": [{"hooks": [{"type": "command", "command": "never"}]}]},
        {"never": ex},
        tmp_path=tmp_path,
    )
    assert mgr.has_hooks_for_event("SessionStart")
    assert not mgr.has_hooks_for_event("PreToolUse")  # Stage 0:未配置
    r = await mgr.dispatch("PreToolUse", input=base_input({"tool_name": "Bash"}))
    assert r.permission_decision is None and r.updated_input is None
    assert ex.calls == 0
    assert not (tmp_path / "hooks.jsonl").exists()  # 零审计


async def test_dedup_same_hook_across_groups(tmp_path):
    """执行层去重(§4.10.3):同一钩子被多个 matcher 组命中 → 只执行一次、只审计一次。"""
    ex = FakeExecutor("guard", [json_result({"permissionDecision": "allow"})])
    mgr = build_manager(
        {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]},
                {"matcher": "Write", "hooks": [{"type": "command", "command": "guard"}]},
            ]
        },
        {"guard": ex},
        tmp_path=tmp_path,
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert ex.calls == 1
    assert r.permission_decision == "allow"
    lines = (tmp_path / "hooks.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # 只审计一次(§8.1 不变量)


async def test_dedup_keeps_last_occurrence_position():
    """去重保留配置序靠后者(last-wins,§4.10.3):重复钩子只执行一次,且位置 = 末次出现处。"""
    order: list[str] = []

    class OrderingExecutor(FakeExecutor):
        async def run(self, input_json, *, timeout):
            order.append(self.name)
            return await super().run(input_json, timeout=timeout)

    ex_x = OrderingExecutor("x", [json_result({})])
    ex_y = OrderingExecutor("y", [json_result({})])
    mgr = build_manager(
        {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "y"}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]},
            ]
        },
        {"x": ex_x, "y": ex_y},
    )
    await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert ex_x.calls == 1 and ex_y.calls == 1
    # x 的两次声明去重为一次,执行位置 = 末次出现(x 在 y 之后),非首次出现
    assert order == ["y", "x"]


async def test_dedup_different_if_not_deduped():
    """不同 if 不去重(§4.10.3 key 含 if):命令相同 if 不同 = 不同钩子。"""
    ex = FakeExecutor("cmd")
    mgr = build_manager(
        {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "cmd", "if": "Bash(git*)"}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "cmd", "if": "Bash(git *)"}]},
            ]
        },
        {"cmd": ex},
        registry=ToolRegistry(get_builtin_tools()),
    )
    await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash", {"command": "git status"})))
    assert ex.calls == 2  # 两条 if 均命中且互不相同 → 不去重


async def test_matcher_group_filter():
    """matcher 组级过滤:不命中 → 组内钩子不 spawn(§2.3/§4.10.2)。"""
    ex = FakeExecutor("guard", [json_result({"permissionDecision": "deny"})])
    mgr = build_manager(
        {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]},
        {"guard": ex},
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Read", {"file_path": "/a"})))
    assert ex.calls == 0
    assert r.permission_decision is None  # 无匹配钩子 → 无决策


async def test_if_rule_filters_before_spawn():
    """if hook 级过滤(spawn 前):不匹配 → 不执行、不进审计(§2.4)。"""
    ex = FakeExecutor("guard", [json_result({"permissionDecision": "deny"})])
    mgr = build_manager(
        {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard", "if": "Bash(git *)"}]}
            ]
        },
        {"guard": ex},
        registry=ToolRegistry(get_builtin_tools()),
    )
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash", {"command": "rm -rf /"})))
    assert ex.calls == 0
    assert r.permission_decision is None
    r2 = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash", {"command": "git status"})))
    assert ex.calls == 1
    assert r2.permission_decision == "deny"


async def test_non_evaluable_event_with_if_never_runs():
    """非可求值事件(Stop/UserPromptSubmit 等)带 if → 永不执行(§2.4,与 test_if_rules.py:139 同语义)。"""
    ex = FakeExecutor("st", [json_result({"continue": False, "stopReason": "r"})])
    mgr = build_manager(
        {"Stop": [{"hooks": [{"type": "command", "command": "st", "if": "Bash(git *)"}]}]},
        {"st": ex},
        registry=ToolRegistry(get_builtin_tools()),
    )
    r = await mgr.dispatch("Stop", input=base_input({"stop_hook_active": True, "stop_reason": "user"}))
    assert ex.calls == 0
    assert r.stop is False


async def test_ignored_matcher_events_run_all():
    """UserPromptSubmit/Stop 带 matcher 也不生效:整组照跑(§2.3)。"""
    ex = FakeExecutor("up", [json_result({"updatedPrompt": "rewritten"})])
    mgr = build_manager(
        {"UserPromptSubmit": [{"matcher": "never-matches", "hooks": [{"type": "command", "command": "up"}]}]},
        {"up": ex},
    )
    r = await mgr.dispatch("UserPromptSubmit", input=base_input({"prompt": "hello"}))
    assert ex.calls == 1
    assert r.updated_prompt == "rewritten"


async def test_lazy_json_shared_across_batch():
    """惰性 JSON 序列化(§4.10.4):同批钩子收到同一序列化结果(只 stringify 一次)。"""
    ex1 = FakeExecutor("one")
    ex2 = FakeExecutor("two")
    mgr = build_manager(
        {"PostToolUse": [{"hooks": [{"type": "command", "command": "one"}, {"type": "command", "command": "two"}]}]},
        {"one": ex1, "two": ex2},
    )
    await mgr.dispatch("PostToolUse", input=base_input({"tool_name": "Bash", "tool_response": {"content": "ok", "is_error": False}}))
    assert ex1.inputs == ex2.inputs == [ex1.inputs[0]]
    payload = json.loads(ex1.inputs[0])
    assert payload["session_id"] == "s1"
    assert payload["tool_name"] == "Bash"
    assert payload["tool_response"] == {"content": "ok", "is_error": False}


# ---------------------------------------------------------------------------
# 聚合传递链(§4.10.6 逐事件消费总表)

async def test_additional_context_join():
    """SessionStart additionalContext 多钩子 join('\n\n')(§7.1)。"""
    ex1 = FakeExecutor("a", [json_result({"additionalContext": "ctx A"})])
    ex2 = FakeExecutor("b", [json_result({"additionalContext": "ctx B"})])
    mgr = build_manager(
        {"SessionStart": [{"hooks": [{"type": "command", "command": "a"}, {"type": "command", "command": "b"}]}]},
        {"a": ex1, "b": ex2},
    )
    r = await mgr.dispatch("SessionStart", input=base_input({"source": "startup", "model": "main"}))
    assert r.additional_context == "ctx A\n\nctx B"


async def test_updated_system_reminder_join():
    """UserPromptSubmit updatedSystemReminder 多钩子 join('\n\n')(§7.2)。"""
    ex1 = FakeExecutor("r1", [json_result({"updatedSystemReminder": "remember A"})])
    ex2 = FakeExecutor("r2", [json_result({"updatedSystemReminder": "remember B"})])
    mgr = build_manager(
        {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "r1"}, {"type": "command", "command": "r2"}]}]},
        {"r1": ex1, "r2": ex2},
    )
    r = await mgr.dispatch("UserPromptSubmit", input=base_input({"prompt": "hi"}))
    assert r.updated_system_reminder == "remember A\n\nremember B"


async def test_updated_prompt_last_wins():
    """UserPromptSubmit updatedPrompt 替换提交文本(§7.1;多钩子 last-wins)。"""
    ex1 = FakeExecutor("p1", [json_result({"updatedPrompt": "first"})])
    ex2 = FakeExecutor("p2", [json_result({"updatedPrompt": "second"})])
    mgr = build_manager(
        {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "p1"}, {"type": "command", "command": "p2"}]}]},
        {"p1": ex1, "p2": ex2},
    )
    r = await mgr.dispatch("UserPromptSubmit", input=base_input({"prompt": "original"}))
    assert r.updated_prompt == "second"


async def test_user_prompt_exit_2_blocks_submit():
    """UserPromptSubmit exit 2 → 阻止提交:blocking_error = stderr,输入擦除(§4.3)。"""
    ex = FakeExecutor("blocker", [HookResult(exit_code=2, stderr="input violates policy")])
    mgr = build_manager(
        {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "blocker"}]}]},
        {"blocker": ex},
    )
    r = await mgr.dispatch("UserPromptSubmit", input=base_input({"prompt": "do a bad thing"}))
    assert r.blocking_error == "input violates policy"
    assert r.updated_prompt is None


async def test_stop_continue_false_stops():
    """Stop continue:false + stopReason → stop + stop_reason(§6.4)。"""
    ex = FakeExecutor("stop", [json_result({"continue": False, "stopReason": "task complete"})])
    mgr = build_manager(
        {"Stop": [{"hooks": [{"type": "command", "command": "stop"}]}]},
        {"stop": ex},
    )
    r = await mgr.dispatch("Stop", input=base_input({"reason": "completed"}))
    assert r.stop is True
    assert r.stop_reason == "task complete"


async def test_stop_exit_2_feedback():
    """Stop exit 2 → stop_feedback(stderr 注入 feedback 继续循环,§6.4)。"""
    ex = FakeExecutor("hold", [HookResult(exit_code=2, stderr="one more thing")])
    mgr = build_manager(
        {"Stop": [{"hooks": [{"type": "command", "command": "hold"}]}]},
        {"hold": ex},
    )
    r = await mgr.dispatch("Stop", input=base_input({"reason": "completed"}))
    assert r.stop_feedback == "one more thing"
    assert r.stop is False


async def test_precompact_instructions_join():
    """PreCompact exit 0 + stdout → 多钩子 join('\n\n') 注入摘要 prompt(§7.4,无 JSON 解析)。"""
    ex1 = FakeExecutor("i1", [HookResult(exit_code=0, stdout="keep the API section")])
    ex2 = FakeExecutor("i2", [HookResult(exit_code=0, stdout="keep the design decisions")])
    mgr = build_manager(
        {"PreCompact": [{"hooks": [{"type": "command", "command": "i1"}, {"type": "command", "command": "i2"}]}]},
        {"i1": ex1, "i2": ex2},
    )
    r = await mgr.dispatch("PreCompact", input=base_input({"trigger": "auto", "context_tokens": 1000}))
    assert r.compact_instructions == "keep the API section\n\nkeep the design decisions"


async def test_precompact_exit_2_blocks_compaction():
    """PreCompact exit 2 → 阻止本轮压缩(§6.2)。"""
    ex = FakeExecutor("block", [HookResult(exit_code=2, stderr="not yet")])
    mgr = build_manager(
        {"PreCompact": [{"hooks": [{"type": "command", "command": "block"}]}]},
        {"block": ex},
    )
    r = await mgr.dispatch("PreCompact", input=base_input({"trigger": "auto"}))
    assert r.block_compact is True


async def test_postcompact_observable_exit_2_ignored():
    """PostCompact 纯观察型:exit 2 与非阻塞等同,无效果(§4.3/§4.10.6 表)。"""
    ex = FakeExecutor("obs", [HookResult(exit_code=2, stderr="whatever")])
    mgr = build_manager(
        {"PostCompact": [{"hooks": [{"type": "command", "command": "obs"}]}]},
        {"obs": ex},
    )
    r = await mgr.dispatch("PostCompact", input=base_input({"trigger": "auto", "compact_summary": "s"}))
    assert r.block_compact is False
    assert r.blocking_error is None


async def test_sessionstart_http_disabled(tmp_path):
    """§4.9 事件适配:仅 SessionStart 禁用 http 执行体(其余事件允许)。"""
    ex = FakeExecutor("http_hook", [json_result({"additionalContext": "ctx"})])
    cfg = {"SessionStart": [{"hooks": [{"type": "http", "url": "http://127.0.0.1:8000/h"}]}]}
    mgr = build_manager(
        cfg, {"http://127.0.0.1:8000/h": ex}, tmp_path=tmp_path,
        http_hook_urls=["http://127.0.0.1:8000/*"],
    )
    r = await mgr.dispatch("SessionStart", input=base_input({"source": "startup"}))
    assert ex.calls == 0  # SessionStart 不执行 http 钩子
    assert r.additional_context is None
    # 其余事件允许(执行体隔离由 test_http.py 的 MockTransport 覆盖,此处仅验证分发层)
    cfg2 = {"PostToolUse": [{"hooks": [{"type": "http", "url": "http://127.0.0.1:8000/h"}]}]}
    mgr2 = build_manager(
        cfg2, {"http://127.0.0.1:8000/h": ex}, http_hook_urls=["http://127.0.0.1:8000/*"]
    )
    await mgr2.dispatch("PostToolUse", input=base_input({"tool_name": "Bash"}))
    assert ex.calls == 1


# ---------------------------------------------------------------------------
# abort 感知(§6.3)

async def test_abort_set_before_dispatch_no_run(tmp_path):
    """入口 abort 置位 → 整批跳过,不产生决策、不审计(§6.3)。"""
    ex = FakeExecutor("hook", [json_result({"permissionDecision": "allow"})])
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "hook"}]}]},
        {"hook": ex},
        tmp_path=tmp_path,
    )
    abort = asyncio.Event()
    abort.set()
    r = await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")), abort_event=abort)
    assert r.permission_decision is None
    assert ex.calls == 0
    assert not (tmp_path / "hooks.jsonl").exists()


async def test_abort_mid_batch_skips_remaining(tmp_path):
    """批次中 abort 置位 → 跳过剩余钩子,记 cancelled(§6.3/§8.1)。"""
    abort = asyncio.Event()

    class AbortAfterRun(FakeExecutor):
        async def run(self, input_json, *, timeout):
            result = await super().run(input_json, timeout=timeout)
            abort.set()  # 第一个钩子执行后置位
            return result

    ex1 = AbortAfterRun("first", [json_result({})])
    ex2 = FakeExecutor("second", [json_result({})])
    ex3 = FakeExecutor("third", [json_result({})])
    mgr = build_manager(
        {"Stop": [{"hooks": [{"type": "command", "command": c}]} for c in ("first", "second", "third")]},
        {"first": ex1, "second": ex2, "third": ex3},
        tmp_path=tmp_path,
    )
    r = await mgr.dispatch("Stop", input=base_input({"reason": "completed"}), abort_event=abort)
    assert ex1.calls == 1 and ex2.calls == 0 and ex3.calls == 0
    outcomes = [json.loads(line)["outcome"] for line in (tmp_path / "hooks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert outcomes == ["success", "cancelled", "cancelled"]


# ---------------------------------------------------------------------------
# 双流审计(§8.1)

async def test_permission_audit_allow_and_deny(tmp_path):
    """钩子决策 → 权限流恰一条(source=hook:PreToolUse);无决策 → 无权限事件。"""
    ex_allow = FakeExecutor("ok", [json_result({"permissionDecision": "allow"})])
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "ok"}]}]},
        {"ok": ex_allow},
        tmp_path=tmp_path,
    )
    await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    evt = json.loads(lines[0])
    assert evt["tool_name"] == "Bash"
    assert evt["decision"] == "allow"
    assert evt["source"] == "hook:PreToolUse"
    assert "hook allow by ok" in evt["reason"]

    ex_deny = FakeExecutor("no", [json_result({"permissionDecision": "deny", "permissionDecisionReason": "bad"})])
    mgr2 = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "no"}]}]},
        {"no": ex_deny},
        tmp_path=tmp_path,
    )
    await mgr2.dispatch("PreToolUse", input=base_input(tool_input_extra("Read", {"file_path": "/a"})))
    evt = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert evt["decision"] == "deny"
    assert "Permission denied by hook no: bad" in evt["reason"]


async def test_no_decision_no_permission_audit(tmp_path):
    """无决策(全部 passthrough)→ 钩子不产生权限审计事件(§5.2 表)。"""
    ex = FakeExecutor("quiet")
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "quiet"}]}]},
        {"quiet": ex},
        tmp_path=tmp_path,
    )
    await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    assert not (tmp_path / "audit.jsonl").exists()
    # 执行流照常记录
    assert len((tmp_path / "hooks.jsonl").read_text(encoding="utf-8").splitlines()) == 1


async def test_immune_recorded_in_permission_audit(tmp_path):
    """免疫位设置 → 审计事件记录(§5.5 约束 4:豁免可追溯)。"""
    ex = FakeExecutor("imm", [json_result({"permissionDecision": "allow", "immune": True})])
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "imm"}]}]},
        {"imm": ex},
        tmp_path=tmp_path,
    )
    await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    evt = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert evt["decision"] == "allow"
    assert "[immune=true]" in evt["reason"]  # ToolAuditEvent 无 immune 字段,reason 标记(见 registry.py 报告注释)


async def test_hook_audit_event_fields(tmp_path):
    """HookAuditEvent 字段完整性(§8.1):event/hook_type/command/outcome/exit_code/duration。"""
    ex = FakeExecutor("guard", [HookResult(exit_code=2, stderr="blocked by guard", duration_ms=42)])
    mgr = build_manager(
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "guard"}]}]},
        {"guard": ex},
        tmp_path=tmp_path,
    )
    await mgr.dispatch("PreToolUse", input=base_input(tool_input_extra("Bash")))
    evt = json.loads((tmp_path / "hooks.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert evt["event"] == "PreToolUse"
    assert evt["hook_type"] == "command"
    assert evt["command"] == "guard"
    assert evt["matched"] is True
    assert evt["outcome"] == "blocked"
    assert evt["exit_code"] == 2
    assert evt["duration_ms"] == 42
    assert evt["stderr_summary"] == "blocked by guard"
    assert evt["timestamp"]


# ---------------------------------------------------------------------------
# notify(§2.5):分发 / 超时覆盖 / fail-open / 审计红线

def _notify_cfg():
    return {
        "Notification": [
            {"matcher": "tool_error", "hooks": [{"type": "command", "command": "err"}]},
            {"hooks": [{"type": "command", "command": "all"}]},  # matcher None = 全匹配
        ]
    }


async def test_notify_matches_notification_type():
    """notify 分发:matcher 取 notification_type(§2.5)。"""
    ex_err = FakeExecutor("err")
    ex_all = FakeExecutor("all")
    mgr = build_manager(_notify_cfg(), {"err": ex_err, "all": ex_all})
    await mgr.notify("tool_error", "boom", title="t", session_id="s1", cwd="/w", session_path="/w/s.jsonl")
    assert ex_err.calls == 1
    assert ex_all.calls == 1
    payload = json.loads(ex_err.inputs[0])
    assert payload["notification_type"] == "tool_error"
    assert payload["message"] == "boom"
    assert payload["title"] == "t"
    assert payload["session_id"] == "s1"

    await mgr.notify("llm_error", "model down")
    assert ex_err.calls == 1  # matcher=llm_error 不命中
    assert ex_all.calls == 2


async def test_notify_default_timeout_10s_override():
    """通知事件默认超时 10s 覆盖执行体默认(§4.2);逐钩子显式 timeout 仍生效。"""
    ex_default = FakeExecutor("d")
    ex_explicit = FakeExecutor("e")
    mgr = build_manager(
        {
            "Notification": [
                {"hooks": [{"type": "command", "command": "d"}]},  # 默认 timeout 60 → 覆盖为 10
                {"hooks": [{"type": "command", "command": "e", "timeout": 5}]},  # 显式 5 → 生效
            ]
        },
        {"d": ex_default, "e": ex_explicit},
    )
    await mgr.notify("permission_request", "approve?")
    assert ex_default.timeouts == [NOTIFICATION_TIMEOUT]
    assert ex_explicit.timeouts == [5.0]
    assert DEFAULT_TIMEOUTS["command"] == 60  # 执行体默认未被破坏


async def test_notify_fail_open_on_hook_failure(tmp_path):
    """通知全事件 fail-open:钩子超时/异常仅记录,不抛给调用方(§2.5)。"""
    ex = FakeExecutor("broken", [TimeoutError("hung")])
    mgr = build_manager(
        {"Notification": [{"hooks": [{"type": "command", "command": "broken"}]}]},
        {"broken": ex},
        tmp_path=tmp_path,
    )
    await mgr.notify("permission_denied", "no")  # 不抛异常
    outcome = json.loads((tmp_path / "hooks.jsonl").read_text(encoding="utf-8").splitlines()[0])["outcome"]
    assert outcome == "timeout"
    assert not (tmp_path / "audit.jsonl").exists()  # 通知不产生权限审计事件(§9.2 红线)


async def test_notify_no_hooks_noop():
    """未配置 Notification 钩子 → notify 零路径直接返回(§4.10.1 Stage 0)。"""
    mgr = build_manager(
        {"SessionStart": [{"hooks": [{"type": "command", "command": "x"}]}]},
        {"x": FakeExecutor("x")},
    )
    await mgr.notify("tool_error", "boom")  # 不抛、不执行
