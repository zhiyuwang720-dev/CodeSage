"""提示执行体(阶段 09,S10):单轮 LLM 调用,§4.8。

- 执行形态:LLMClient.complete 单轮、无工具能力(§4.7)、默认超时 30s(§4.2);
- `$ARGUMENTS` 占位符替换为 HookInput JSON(§4.1/§4.10.4,CC hookHelpers.ts:30-35 最小版);
- 模型指针:spec.model 缺省 `"quick"`,失败自动回退 main —— 既有机制在
  LLMClient.complete(辅助请求回退,client.py:121-142),本类只传指针、不重复实现回退;
- 输出契约:模型响应必须是 `{ok, reason}` JSON。LLMRequest 无 json_schema /
  response_format 通道(adapter 不支持),「强制 JSON」以系统提示 + 客户端严格校验
  落地:输出不可解析或字段缺失 → HookValidationError → §4.6 表 fail-closed
  (PreToolUse → deny,Stop → 放行 + warning)——安全语义不缩水,仅缺 provider 侧
  schema 强制(与 spec §4.8「请求以 json_schema 强制」的落地差异,见 S10 报告);
- `{ok,reason}` → HookResult 映射(§4.8/§4.10.6):ok:true → exit 0 + 空 stdout
  (plainText,无决策);ok:false → exit 2 + stderr=reason —— 消费动作同 exit 2 行,
  由 S5 按事件区分(PreToolUse → deny 且 reason 进拒绝文案与审计 / Stop → 阻止
  停止 / UserPromptSubmit → 阻止提交 / PreCompact → 阻止压缩 / 观察型事件 → 非
  阻塞)。`{ok,reason}` 不是 HookJSONOutput 字段(unknown field → 校验失败),故不
  落 stdout,经退出码通道映射,S5 零新增合并逻辑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..ai import LLMError, LLMRequest, Message
from .base import HookResult
from .command import HookExecutionError
from .types import HookValidationError

logger = logging.getLogger("codesage.hooks")

#: 系统提示(§4.8):只输出 {ok, reason} JSON。对齐 CC execPromptHook.ts:64-70 的
#: systemPrompt(json_schema 的客户端校验侧对应物;schema:ok required bool,
#: reason optional str,additionalProperties: false)。
SYSTEM_PROMPT = (
    "你是 CodeSage 的钩子评估器。你的回答必须是一个 JSON 对象,且只能输出 JSON"
    "(不要输出任何其他文本),符合以下 schema 之一:\n"
    '1. 条件满足:{"ok": true}\n'
    '2. 条件不满足:{"ok": false, "reason": "不满足的原因"}'
)


def render_prompt(template: str, input_json: str) -> str:
    """$ARGUMENTS 占位符替换(§4.1/§4.10.4):所有出现替换为 HookInput JSON。

    CC hookHelpers.ts:30-35 的最小版:不做 `$ARGUMENTS[n]` 索引(刻意裁剪,§4.1)。
    """
    return template.replace("$ARGUMENTS", input_json)


def parse_hook_output(text: str) -> tuple[bool, str | None]:
    """{ok,reason} 解析与校验(§4.8,json_schema 的客户端校验落地)。

    规则(对齐 CC execPromptHook 的 json_schema:ok required bool、reason optional
    str、additionalProperties: false):
    - `ok` 必填且必须 bool —— 缺失/非 bool → HookValidationError(fail-closed);
    - `reason` 可选,提供时必须 str;ok:false 无 reason → 空文案,deny 仍生效
      (阻塞信号不因文案缺失而丢失);
    - 空响应 / 非 JSON / 非对象 / 未知字段 → HookValidationError(fail-closed)。
    """
    if not text.strip():
        raise HookValidationError("prompt hook returned an empty response (§4.8)")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HookValidationError(f"prompt hook output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HookValidationError(
            f"prompt hook output must be a JSON object, got {type(data).__name__}"
        )
    unknown = set(data) - {"ok", "reason"}
    if unknown:
        raise HookValidationError(
            f"prompt hook output has unknown fields {sorted(unknown)} (additionalProperties: false)"
        )
    ok = data.get("ok")
    if not isinstance(ok, bool):
        raise HookValidationError(
            f"prompt hook output field 'ok' must be a boolean, got {ok!r}"
        )
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise HookValidationError(
            f"prompt hook output field 'reason' must be a string, "
            f"got {type(reason).__name__}"
        )
    return ok, reason


class PromptHookExecutor:
    """提示执行体(§4.8):单轮 LLM 调用 + $ARGUMENTS 替换 + {ok,reason} 契约。

    client 为 LLMClient(或实现 `complete(request, *, model)` 的对象);None = 未
    装配,运行即 fail-closed(HookExecutionError,§4.6 表 spawn 失败同档)。model
    缺省 `"quick"` 指针(§3.1),失败自动回退 main 由 LLMClient.complete 的既有
    辅助请求回退承担,本类不重复实现。超时 / 校验失败 / LLM 调用失败抛异常不构造
    HookResult(base.py 契约),S5 按 §4.6 表 fail-closed。
    """

    def __init__(
        self, prompt: str, *, client: Any | None = None, model: str | None = None
    ) -> None:
        self.prompt = prompt
        self.client = client
        self.model = model

    async def run(self, input_json: str, *, timeout: float) -> HookResult:
        if self.client is None:
            raise HookExecutionError(
                f"prompt hook requires an LLM client (not wired at assembly): {self.prompt!r}"
            )
        request = LLMRequest(
            messages=[
                Message(role="user", content=render_prompt(self.prompt, input_json))
            ],
            system=SYSTEM_PROMPT,
        )
        started = time.monotonic()
        try:
            # 单个 wait_for 覆盖 LLM 调用全程(§4.2;LLMClient 内部重试与 quick→main
            # 回退也在其内,超时统一按 §4.6 表 fail-closed)
            response = await asyncio.wait_for(
                self.client.complete(request, model=self.model or "quick"), timeout
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"prompt hook timed out after {timeout:.1f}s: {self.prompt!r}"
            ) from None
        except LLMError as exc:
            # LLM 调用失败(回退 main 后仍失败)→ 执行体级失败(§4.6 表 spawn 失败同档)
            raise HookExecutionError(f"prompt hook LLM call failed: {exc}") from exc
        if response.is_error:
            # 错误被既有链路渲染为响应(LLMResponse.is_error)→ 同 LLM 调用失败
            raise HookExecutionError(
                "prompt hook LLM call failed: "
                + (response.error_message or "unknown provider error")
            )
        ok, reason = parse_hook_output(response.text)
        if ok:
            # ok:true → 无决策(§4.8):exit 0 + 空 stdout → S5 走 plainText 分支,
            # 引擎照常求值。原始 {ok,...} 文本不进 stdout:它会被 S5 当 HookJSONOutput
            # 解析(unknown field → 校验失败),且本就无消费方。
            return HookResult(exit_code=0, duration_ms=int((time.monotonic() - started) * 1000))
        # ok:false → 阻塞信号(§4.8/§4.10.6):消费动作同 exit 2 行 —— PreToolUse
        # deny(reason 经 deny_reason 进拒绝文案与权限流审计)/ Stop 阻止停止
        # (feedback 注入继续循环)/ UserPromptSubmit 阻止提交,由 S5 _merge_blocked
        # 按事件区分,本类不感知事件名(http.py 同哲学)。
        return HookResult(
            exit_code=2,
            stderr=reason or "",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
