"""HookManager 执行引擎(阶段 09,S5):§4.10 统一管线 + §5.2 决策合并 + 双流审计。

管线落地(docs/specs/09-hooks.md §4.10):
- Stage 0 快速存在性检查:事件 → 钩子数索引,索引空 → 零路径(§4.10.1);
- Stage 2 匹配与收集:matcher 组级(§2.3)→ if hook 级(§2.4),均在 spawn 前;
- Stage 3 执行层去重:key = (type, command|prompt|url, if),last-wins(§4.10.3);
- Stage 4 输入构建:同一批次共享一次 JSON 序列化(§4.10.4 惰性);
- Stage 5 输出解析:退出码分类 + stdout JSON/plainText 分支,失败按 §4.6 fail-closed;
- Stage 6 结果聚合:逐事件消费总表(§4.10.6)——决策合并(§5.2)/消息改写(§7.1)/
  Stop 门控(§6.4)/PreCompact 指令 join(§7.4);
- 审计(§8.1):执行流 hooks.jsonl 每钩子恰好一条 + 权限流 audit.jsonl 仅 PreToolUse
  产生决策时一条。

与 spec 的偏差/取舍(向 lead 如实汇报):
1. prompt 执行体 S10 交付;本步工厂注册占位执行体,任何调用抛 HookExecutionError
   → 按 §4.6 fail-closed(PreToolUse → deny,其他事件 → 非阻塞),不会静默跳过。
2. 免疫位审计(§5.5 约束 4):ToolAuditEvent 无 immune 字段(audit.py 不改),allow
   事件 reason 追加 ` [immune=true]` 标记。
3. SessionStart 禁用的 http 钩子(§4.9)在收集期剔除,不产生审计事件(从未被调用)。
4. exit 2 的聚合只取首个(顺序模型首个阻塞信号;仅 PreToolUse deny 短路,§4.10.6)。
5. 通知基础三字段经 notify 的 **data 传入,缺省空字符串(§2.5 HookInput 字段清单)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from ..permissions.audit import AuditSink, NullAuditSink, ToolAuditEvent
from ..tools import ToolRegistry, get_builtin_tools
from ._common import HookGroup, if_rule_matches, match_matcher, parse_hook_config
from .base import HookResult
from .command import (
    CommandHookExecutor,
    HookExecutionError,
    classify_exit_code,
    parse_hook_stdout,
)
from .http import HttpHookExecutor
from .types import (
    DEFAULT_TIMEOUTS,
    MATCHER_IGNORED_EVENTS,
    NOTIFICATION_TYPES,
    HookAuditEvent,
    HookInput,
    HookSpec,
    HookValidationError,
)

logger = logging.getLogger("codesage.hooks")

#: 通知事件整体超时覆盖(§4.2 表):默认 10s,逐钩子显式 timeout 仍覆盖。
#: HookSpec 无法区分「显式配 60」与「默认 60」,以等于 DEFAULT_TIMEOUTS 视为默认 → 覆盖。
NOTIFICATION_TIMEOUT = 10.0

#: 每事件 matcher 匹配值取法(§2.2 事件表匹配值列;UserPromptSubmit/Stop 不匹配,§2.3)
_MATCH_VALUE_KEYS = {
    "PreToolUse": "tool_name",
    "PostToolUse": "tool_name",
    "SessionStart": "source",
    "PreCompact": "trigger",
    "PostCompact": "trigger",
    "Notification": "notification_type",
}


@dataclass(slots=True)
class HookDispatchResult:
    """dispatch() 聚合产物(§4.10.6 逐事件消费总表)。

    事件无关字段全量声明(默认 None/False),S6-S8 按事件消费:
    - PreToolUse:permission_decision / deny_reason / deny_hook / hook_allowed / immune / updated_input
    - UserPromptSubmit:blocking_error / updated_prompt / updated_system_reminder / additional_context
    - SessionStart:additional_context
    - Stop:stop / stop_reason / stop_feedback
    - PreCompact:block_compact / compact_instructions
    """

    event: str
    # PreToolUse(§5.2)
    permission_decision: str | None = None  # "allow" | "deny" | None(引擎照常)
    deny_reason: str | None = None  # "Permission denied by hook {name}: {reason}"
    deny_hook: str | None = None
    allow_hook: str | None = None  # allow 决策钩子名(§8.1 审计 reason 用)
    hook_allowed: bool = False  # allow 短路位 → item.hook_allowed(§5.2)
    immune: bool = False  # safetyCheck bypass 免疫位(§5.5)
    updated_input: dict[str, Any] | None = None  # last-wins(§5.4;deny 钩子的改写不生效)
    # UserPromptSubmit / SessionStart(§7.1)
    blocking_error: str | None = None  # exit 2 stderr → 阻止提交(§4.3)
    updated_prompt: str | None = None  # last-wins
    updated_system_reminder: str | None = None  # 多钩子 join('\n\n')(§7.2)
    additional_context: str | None = None  # 多钩子 join('\n\n')(§7.1)
    # Stop(§6.4)
    stop: bool = False  # continue:false → _stop("hook", stop_reason)
    stop_reason: str | None = None
    stop_feedback: str | None = None  # exit 2 stderr → 注入 feedback 消息继续循环
    # PreCompact(§6.2/§7.4)
    block_compact: bool = False  # exit 2 → 阻止本轮压缩
    compact_instructions: str | None = None  # exit 0 stdout 多钩子 join('\n\n')


def _match_value(event: str, input: HookInput) -> str | None:
    """每事件 matcher 匹配值(§2.2):UserPromptSubmit/Stop 返回 None(matcher 不生效)。"""
    if event in MATCHER_IGNORED_EVENTS:
        return None
    key = _MATCH_VALUE_KEYS.get(event)
    if key is None:
        return None
    extra = input.extra or {}
    value = extra.get(key)
    return value if isinstance(value, str) else None


def _hook_name(spec: HookSpec, limit: int = 200) -> str:
    """钩子标识摘要(§8.1 command 字段:命令/prompt/url,截断 200 字符)。"""
    name = spec.command or spec.prompt or spec.url or ""
    return name if len(name) <= limit else name[:limit] + "..."


def _dedup_key(spec: HookSpec) -> tuple[str, str, str | None]:
    """执行层去重 key(§4.10.3):(type, command|prompt|url, if)。"""
    payload = spec.command or spec.prompt or spec.url or ""
    return (spec.type, payload, spec.if_)


class _PromptUnavailableExecutor:
    """prompt 执行体占位(S10 交付前):调用即 fail-closed(§4.6),不静默放行。

    S10 交付 prompt.py 后,load_hook_manager 的默认工厂替换为 PromptHookExecutor。
    """

    def __init__(self, prompt: str):
        self.prompt = prompt

    async def run(self, input_json: str, *, timeout: float) -> HookResult:
        raise HookExecutionError(
            f"prompt executor not implemented until phase 10 (S10): hook {self.prompt!r}"
        )


class HookManager:
    """事件分发器(实现 base.py 的 HookManager 协议,§4.10 统一管线)。

    构造:parse_hook_config 产物(事件 → 组 → 钩子)+ 执行体工厂(事件 → 执行体实例
    映射,构造期一次装配)。快照语义(§3.2):构造后配置不热载。
    """

    def __init__(
        self,
        groups: dict[str, list[HookGroup]],
        *,
        executor_factory: Callable[[HookSpec], Any],
        audit: AuditSink | None = None,
        hooks_sink: Any | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._groups = groups
        # Stage 0 索引(§4.10.1):事件 → 钩子数,构建于解析期,随快照冻结
        self._index = {
            event: sum(len(g.hooks) for g in event_groups)
            for event, event_groups in groups.items()
        }
        self._executors = {
            id(spec): executor_factory(spec)
            for event_groups in groups.values()
            for group in event_groups
            for spec in group.hooks
        }
        self._audit = audit if audit is not None else NullAuditSink()
        self._hooks_sink = hooks_sink if hooks_sink is not None else NullAuditSink()
        self._registry = registry  # if 条件求值(§2.4);None = 视为无工具,if 恒 false

    # ------------------------------------------------------------------
    # 协议:has_hooks_for_event / dispatch / notify(§4.10 流水线总序)

    def has_hooks_for_event(self, event: str) -> bool:
        """Stage 0 快速存在性检查(§4.10.1):索引空 → 零路径,不进管线。"""
        return self._index.get(event, 0) > 0

    async def dispatch(
        self, event: str, *, input: HookInput, abort_event: asyncio.Event | None = None
    ) -> HookDispatchResult:
        """执行一次事件的完整管线(匹配 → 去重 → 顺序执行 → 聚合)。"""
        return await self._run_pipeline(event, input, abort_event=abort_event)

    async def notify(
        self,
        notification_type: str,
        message: str,
        *,
        title: str | None = None,
        **data: Any,
    ) -> None:
        """通知事件(§2.5):全事件 fail-open、默认超时 10s、不参与决策。

        基础三字段(session_id/cwd/session_path)经 **data 传入(§2.5 HookInput 字段
        清单),缺省为空字符串。matcher 取 notification_type;通知不产生权限审计事件
        (§9.2 红线)。任何钩子失败仅日志,不抛给调用方(通知源处于 UI 关键路径)。
        """
        if not self.has_hooks_for_event("Notification"):
            return
        if notification_type not in NOTIFICATION_TYPES:
            logger.warning("unknown notification_type %r (§2.5)", notification_type)
        session_id = data.get("session_id", "")
        cwd = data.get("cwd", "")
        session_path = data.get("session_path", "")
        extra = dict(data)
        for key in ("session_id", "cwd", "session_path"):
            extra.pop(key, None)
        extra["notification_type"] = notification_type
        extra["message"] = message
        if title is not None:
            extra["title"] = title
        hook_input = HookInput(
            session_id=session_id if isinstance(session_id, str) else "",
            cwd=cwd if isinstance(cwd, str) else "",
            session_path=session_path if isinstance(session_path, str) else "",
            extra=extra,
        )
        try:
            await self._run_pipeline(
                "Notification", hook_input, abort_event=None, audit_event=notification_type
            )
        except Exception:
            # fail-open(§2.5):通知钩子异常不拖累权限询问/错误路径,仅日志
            logger.exception("notification hook dispatch failed (fail-open, §2.5)")

    # ------------------------------------------------------------------
    # 内部:统一管线(§4.10)

    async def _run_pipeline(
        self,
        event: str,
        input: HookInput,
        *,
        abort_event: asyncio.Event | None,
        audit_event: str | None = None,
    ) -> HookDispatchResult:
        """统一管线主体。audit_event = 审计流事件标签(notify 用 notification_type,§8.1)。"""
        result = HookDispatchResult(event=event)
        # Stage 0(§4.10.1):无配置事件 → 零路径,不进管线
        if not self.has_hooks_for_event(event):
            return result
        audit_label = audit_event if audit_event is not None else event
        # 入口 abort 检查(§6.3):已置位 → 跳过整批,不产生决策、不审计
        if abort_event is not None and abort_event.is_set():
            return result

        # Stage 4(§4.10.4):输入构建 —— 同批共享一次 JSON 序列化
        input_json = input.to_json()
        extra = input.extra or {}
        tool_name = extra.get("tool_name") if isinstance(extra.get("tool_name"), str) else ""
        tool_input = extra.get("tool_input") if isinstance(extra.get("tool_input"), dict) else {}
        match_value = _match_value(event, input)

        # Stage 2(§4.10.2):matcher 组级 → if hook 级,均在 spawn 前
        batch: list[HookSpec] = []
        for group in self._groups.get(event, []):
            if match_value is not None and not match_matcher(group.matcher, match_value):
                continue
            for spec in group.hooks:
                if event == "SessionStart" and spec.type == "http":
                    # §4.9 事件适配:仅 SessionStart 禁用 http 执行体(关键路径不依赖外网)
                    logger.debug("http hook skipped on SessionStart (§4.9): %r", spec.url)
                    continue
                if spec.if_ is not None:
                    if not spec.if_evaluable:
                        # §2.4:非可求值事件(Stop/UserPromptSubmit 等)带 if
                        # → 永不执行(warning 已由 S1 解析期发出),不 spawn、不进审计
                        continue
                    if self._registry is None or not if_rule_matches(
                        spec.if_, tool_name, tool_input, self._registry
                    ):
                        # §2.4:if 不匹配 → 不 spawn、不进审计
                        continue
                batch.append(spec)

        # Stage 3(§4.10.3):执行层去重 —— key 冲突保留配置序靠后者(last-wins)
        if len(batch) > 1:
            last_idx: dict[tuple[str, str, str | None], int] = {}
            for i, spec in enumerate(batch):
                last_idx[_dedup_key(spec)] = i
            if len(last_idx) < len(batch):
                batch = [batch[i] for i in sorted(last_idx.values())]

        # Stage 5/6(§4.10.5/§4.10.6):顺序执行 + 输出解析 + 聚合
        verdict: str | None = None  # allow | deny | None;deny 是终局(§5.2)
        additions: list[str] = []  # additionalContext 多钩子 join('\n\n')
        reminders: list[str] = []  # updatedSystemReminder 多钩子 join('\n\n')
        instructions: list[str] = []  # PreCompact custom instructions join('\n\n')

        for spec in batch:
            if verdict == "deny":
                # §5.2:deny 是终局,后续钩子不再执行(记 skipped)
                self._audit_hook(audit_label, spec, outcome="skipped", matched=True)
                continue
            if abort_event is not None and abort_event.is_set():
                # §6.3:批次中 abort 置位 → 跳过剩余钩子,不产生决策(记 cancelled)
                self._audit_hook(audit_label, spec, outcome="cancelled", matched=True)
                continue

            executor = self._executors.get(id(spec))
            outcome, exit_code, stderr_summary, hook_result = await self._run_one(
                event, spec, executor, input_json
            )
            failed = hook_result is None  # 异常路径(超时/校验失败/spawn 失败/钩子 bug)

            if hook_result is not None and outcome == "success":
                try:
                    self._merge_success(
                        event, spec, hook_result, result, additions, reminders, instructions
                    )
                except HookValidationError as exc:
                    # stdout 以 `{` 开头但 JSON/校验失败(§4.6 第一行)——归类为
                    # validation_error,fail-closed 由 failed 位驱动
                    logger.warning("hook output validation failed (fail-closed, §4.6): %s", exc)
                    outcome = "validation_error"
                    stderr_summary = str(exc)[:200]
                    failed = True
            elif hook_result is not None and outcome == "blocked":  # exit 2
                self._merge_blocked(event, spec, hook_result, result)
            # exit 1/其他(non_blocking_error):fail-open,流程继续(§4.6 表)

            self._audit_hook(
                audit_label,
                spec,
                outcome=outcome,
                matched=True,
                exit_code=exit_code,
                stderr_summary=stderr_summary,
                duration_ms=hook_result.duration_ms if hook_result is not None else 0,
            )

            if failed and event == "PreToolUse":
                # §4.6 fail-closed:钩子「没能说话」时安全门关闭(deny);
                # exit 1 是钩子作者显式自声明,不在此列
                error = stderr_summary or "hook failed"
                result.permission_decision = "deny"
                result.deny_hook = _hook_name(spec)
                result.deny_reason = (
                    f"Permission denied by hook {_hook_name(spec)}: {error}"
                )
            # 其他事件 + 执行异常 → 非阻塞错误记录,流程继续(§4.6 表)

            if result.permission_decision == "deny":
                verdict = "deny"
            elif result.permission_decision == "allow" and verdict is None:
                verdict = "allow"

        # 聚合传递链:多钩子文本字段 join('\n\n')(§4.10.6/§7.1/§7.4)
        if additions:
            result.additional_context = "\n\n".join(additions)
        if reminders:
            result.updated_system_reminder = "\n\n".join(reminders)
        if instructions:
            result.compact_instructions = "\n\n".join(instructions)

        # allow 短路位(§5.2 表):无 deny 且任一 allow;由终局 verdict 定夺,deny 终局时复位
        result.hook_allowed = verdict == "allow"

        if event == "PreToolUse" and verdict is not None:
            # 权限流审计(§8.1):钩子产生决策时一条,source=hook:PreToolUse;
            # 无决策(全部 passthrough)不产生权限审计事件(§5.2 表)
            if verdict == "deny":
                reason = result.deny_reason
            else:
                reason = f"hook allow by {result.allow_hook or '?'}"
                if result.immune:
                    reason += " [immune=true]"  # §5.5 约束 4:免疫位审计可追溯
            self._audit.emit(
                ToolAuditEvent(
                    tool_name=tool_name,
                    decision=verdict,
                    reason=reason,
                    source="hook:PreToolUse",
                    mode="default",
                    input_summary=None,  # 钩子输入输出内容不落审计(§8.1)
                )
            )
        return result

    async def _run_one(
        self,
        event: str,
        spec: HookSpec,
        executor: Any,
        input_json: str,
    ) -> tuple[str, int | None, str | None, HookResult | None]:
        """单钩子执行(Stage 5):异常按 §4.6 表分类,返回 (outcome, exit_code, stderr, result)。

        失败语义(§4.6):PreToolUse 由调用方转 deny;其他事件 → 非阻塞错误记录。
        """
        timeout = self._timeout_for(event, spec)
        try:
            hook_result = await executor.run(input_json, timeout=timeout)
        except TimeoutError as exc:
            logger.warning("hook timed out (fail-closed, §4.2): %s", exc)
            return "timeout", None, str(exc)[:200], None
        except HookValidationError as exc:
            logger.warning("hook output validation failed (fail-closed, §4.6): %s", exc)
            return "validation_error", None, str(exc)[:200], None
        except HookExecutionError as exc:
            logger.warning("hook execution failed (§4.6): %s", exc)
            return "non_blocking_error", None, str(exc)[:200], None
        except Exception as exc:
            # §6.3:钩子自身 bug(非 HookError)→ 捕获转非阻塞错误,不拖垮主循环
            logger.exception("hook raised unexpected %s: %s", type(exc).__name__, exc)
            return "non_blocking_error", None, str(exc)[:200], None

        outcome = classify_exit_code(hook_result.exit_code)
        if outcome == "success":
            logger.debug(
                "hook executed: event=%s exit=%s duration=%sms outcome=%s",
                event, hook_result.exit_code, hook_result.duration_ms, outcome,
            )
        else:
            logger.warning(
                "hook exit %s (outcome=%s): %s",
                hook_result.exit_code, outcome, hook_result.stderr[:200],
            )
        return outcome, hook_result.exit_code, hook_result.stderr[:200], hook_result

    def _merge_success(
        self,
        event: str,
        spec: HookSpec,
        hook_result: HookResult,
        result: HookDispatchResult,
        additions: list[str],
        reminders: list[str],
        instructions: list[str],
    ) -> None:
        """exit 0 的输出消费(§4.10.6 总表)。"""
        if event == "PreCompact":
            # §7.4:指令输出是纯文本 join,无 JSON 解析(压缩不是安全门)
            if hook_result.stdout:
                instructions.append(hook_result.stdout)
            return
        output, warnings = parse_hook_stdout(hook_result.stdout, event)
        for warning in warnings:
            logger.warning("hook output warning: %s", warning)
        if output is None:
            return  # plainText:仅日志(§4.3)
        if output.systemMessage:
            logger.info("hook systemMessage: %s", output.systemMessage)

        if event == "PreToolUse":
            decision = output.permissionDecision or (
                # §4.4 兼容别名:approve→allow,block→deny(仅 PreToolUse 有意义)
                {"approve": "allow", "block": "deny"}.get(output.decision)
                if output.decision
                else None
            )
            if decision == "deny":
                reason = output.permissionDecisionReason or ""
                result.permission_decision = "deny"
                result.deny_hook = _hook_name(spec)
                result.deny_reason = (
                    f"Permission denied by hook {_hook_name(spec)}"
                    + (f": {reason}" if reason else "")
                )
                # deny 钩子的 updatedInput 不生效(§5.2 守卫)
            elif decision == "allow":
                result.permission_decision = "allow"
                result.allow_hook = _hook_name(spec)
                result.immune = result.immune or output.immune  # §5.5:仅 allow 同结果生效
                if output.updatedInput is not None:
                    result.updated_input = output.updatedInput  # last-wins(§5.4)
            else:  # passthrough:无决策钩子也可改写输入(§5.4)
                if output.updatedInput is not None:
                    result.updated_input = output.updatedInput
        elif event == "UserPromptSubmit":
            if output.updatedPrompt is not None:
                result.updated_prompt = output.updatedPrompt  # last-wins(§7.1)
            if output.updatedSystemReminder is not None:
                reminders.append(output.updatedSystemReminder)
            if output.additionalContext is not None:
                additions.append(output.additionalContext)
        elif event == "SessionStart":
            if output.additionalContext is not None:
                additions.append(output.additionalContext)
        elif event == "Stop":
            # §4.4:continue:false → 停止。types.py 将「缺省」与「显式 false」都折叠为
            # continue_=False(只读不改),故复查原始 JSON 的键存在性:缺省 = 无效果。
            try:
                raw = json.loads(hook_result.stdout)
            except (json.JSONDecodeError, TypeError):
                raw = {}
            if isinstance(raw, dict) and raw.get("continue") is False:
                result.stop = True
                if output.stopReason is not None:
                    result.stop_reason = output.stopReason  # last-wins
        # PostToolUse/PostCompact/Notification:观察型,无改写通道(§4.10.6 表)

    def _merge_blocked(
        self,
        event: str,
        spec: HookSpec,
        hook_result: HookResult,
        result: HookDispatchResult,
    ) -> None:
        """exit 2(blockingError)按事件消费(§4.10.6 总表;首个非空 stderr 生效)。"""
        stderr = hook_result.stderr.strip()
        if event == "PreToolUse":
            result.permission_decision = "deny"
            result.deny_hook = _hook_name(spec)
            result.deny_reason = (
                f"Permission denied by hook {_hook_name(spec)}"
                + (f": {stderr}" if stderr else "")
            )
        elif event == "UserPromptSubmit":
            if result.blocking_error is None and stderr:
                result.blocking_error = stderr  # 阻止提交,输入擦除(§4.3)
        elif event == "Stop":
            if result.stop_feedback is None and stderr:
                result.stop_feedback = stderr  # 注入 feedback 继续循环(§6.4)
        elif event == "PreCompact":
            result.block_compact = True  # 阻止本轮压缩(§6.2)
        # SessionStart/PostToolUse/PostCompact/Notification:exit 2 与非阻塞等同(§4.3/§2.5)

    def _timeout_for(self, event: str, spec: HookSpec) -> float:
        """逐钩子超时(§4.2):Notification 事件默认 10s 覆盖执行体默认,显式配置仍生效。"""
        if event == "Notification" and spec.timeout == DEFAULT_TIMEOUTS.get(spec.type):
            return NOTIFICATION_TIMEOUT
        return float(spec.timeout)

    def _audit_hook(
        self,
        audit_event: str,
        spec: HookSpec,
        *,
        outcome: str,
        matched: bool,
        exit_code: int | None = None,
        stderr_summary: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        """执行流审计(§8.1):每次钩子调用(含 skipped/cancelled)恰好一条。"""
        self._hooks_sink.emit(
            HookAuditEvent(
                event=audit_event,
                hook_type=spec.type,
                command=_hook_name(spec),
                matched=matched,
                outcome=outcome,
                exit_code=exit_code,
                duration_ms=duration_ms,
                stderr_summary=stderr_summary,
                timestamp="",
            )
        )


def load_hook_manager(
    hooks_cfg: Any,
    *,
    client: Any | None = None,
    audit: AuditSink | None = None,
    hooks_sink: Any | None = None,
    http_hook_urls: list[str] | None = None,
    registry: ToolRegistry | None = None,
) -> HookManager:
    """装配入口(§3.2):settings.hooks(已三层合并)→ 解析 → 执行体实例映射。

    快照语义:此处解析一次,会话中 settings.json 修改不生效(§3.2)。hooks.jsonl
    路径 = paths.config_dir() / "hooks.jsonl",由 assemble.py(S10)传入 hooks_sink。
    client 仅 prompt 执行体使用(S10 交付后接管默认工厂)。
    """
    groups = parse_hook_config(hooks_cfg, http_hook_urls=http_hook_urls)
    if registry is None:
        registry = ToolRegistry(get_builtin_tools())
    whitelist = http_hook_urls or []

    def factory(spec: HookSpec) -> Any:
        if spec.type == "command":
            return CommandHookExecutor(spec.command or "")
        if spec.type == "http":
            return HttpHookExecutor(
                spec.url or "",
                headers=spec.headers,
                allowed_env_vars=spec.allowedEnvVars,
                urls_whitelist=whitelist,
            )
        # prompt:S10 交付 PromptHookExecutor(spec.prompt, client=client, model=spec.model)
        return _PromptUnavailableExecutor(spec.prompt or "")

    return HookManager(
        groups,
        executor_factory=factory,
        audit=audit,
        hooks_sink=hooks_sink,
        registry=registry,
    )
