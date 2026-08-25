"""回合与步骤边界的默认 agent 驱动。

循环的因果形状:一次「回合」= turn/start 到 turn/end 的耐久边界,
一个「步骤」= 一次模型请求 + 其工具执行。设计上每次请求**都从
会话日志派生**(消息历史、请求头折叠、运行时上下文快照)—— 没有
与日志平行的内部状态。这类比操作系统的审计日志:日志是
write-ahead 的唯一事实源,内存里的投影(消息历史、游标、相位)
都是可从日志重建的缓存;输入(message)经 Inbox 的耐久 splice
事件排队,取消(cause)经 AbortSignal 沉淀到回合结局 —— 无论
进程如何退出,日志都能重放出同一个世界状态。

**实现差异**(均在注释中标出):

- 无内建 Promise.withResolvers:driver 的完成经
  ``loop.create_future()`` + Task done-callback 转发(裸
  coroutine 的 with_initiator 路径无 loop,无法转发,见
  ``_wake_driver``);
- ``LLMError.failure`` 挂载:llm 包的 LLMError 没有 failure 字段,
  step() 抛出前动态挂 ``error.failure = finish['failure']``,
  turn() 的 error 分类用 ``getattr`` 读取;
- ``deepFreeze``/``structuredClone`` 省略:配置与请求 dict 都是
  刚构造的局部值,无共享引用,防篡改语义不适用;
- 服务访问 ``ctx.llm`` / ``ctx.systemPrompt`` 经 getattr 宽容
  获取(测试用 accessor 注册 stub;服务就绪即属性可用);
- ``stream`` 调用无 per-request cancel:取消在 step 循环的
  ``signal.throw_if_aborted()`` 检查点生效,不中断在飞流
  (llm 客户端无公开 cancel,见 loop_markers 注释)。
"""

from __future__ import annotations

import asyncio
import inspect
import json

from llm.llm.src.assembler import BlockAssembler
from llm.llm.src.error_chain import error_chain
from llm.llm.src.loop_markers import mark_agent_loop_request
from llm.llm.src.messages import create_assistant_message
from llm.llm.src.types import LLMError, LLMRequest
from core.agent.src.dispatch import agent_events, assemble_context_for
from core.agent.src.inbox import Inbox, InboxNotifications
from core.scope.src.index import create_scope
from core.session.src.request_header import canonical_header, header_equals
from system_prompt.system_prompt.src.index import (
    join_context_sections,
    render_context_sections,
    render_prompt,
)

from .abort import AbortController, AbortSignal
from .runtime_context import RuntimeContextProjection
from .tool_calls import execute_tool_calls

__all__ = ["ReactLoopAgent"]

#: 步骤结束原因:只有 completed / max-tokens 会被 turn 采信为
#: 回合结局(aborted/error/blocked 由 turn 自己分类)。
_STEP_END_KINDS = ("completed", "max-tokens")


def _request_proposal(header: dict) -> dict:
    """去掉适配器派生值后的请求配置(插件提出下一个配置前的输入)。

    适配器解析出的默认值(如 reasoningEffort/maxTokens)只属于
    那次解析;配置被插件改写后重放,适配器派生值不再可信,删除
    让它们在下一次解析时重新决议。
    """
    if header.get("adapterDefaults") is None:
        return header["config"]
    proposal = dict(header["config"])
    adapter = header["adapterDefaults"]
    if adapter.get("reasoningEffort") is True:
        proposal.pop("reasoningEffort", None)
    if adapter.get("maxTokens") is True:
        proposal.pop("maxTokens", None)
    return proposal


async def _resolve_waterfall(value):
    """waterfall 链结果统一解析:监听者可能同步返回或返回协程。

    cordis 的 waterfall 是同步中间件链(返回「链结果的原始值」);
    agent 事件的水fall 监听者两种风格都写,消费方一律经本函数
    归一成值。
    """
    return await value if inspect.isawaitable(value) else value


# ---- 词表转换:会话词表 → llm 契约 ----

#: 会话词表块 → llm 词表块;结构是 dict → dict,返回的都是 llm 词表形状
_BLOCK_KIND = {"text": "text", "reasoning": "thinking", "tool-call": "tool_use", "tool-result": "tool_result"}


def _convert_blocks(blocks: list) -> list:
    """把会话词表块列表转换到 llm 词表(ContentBlock 契约)。

    会话词表是 durable 词表(tool-call/tool-result/reasoning),
    llm 契约是请求词表(text/thinking/tool_use/tool_result);
    工具调用参数在边界解析 JSON —— 解析失败保留原文为
    ``_partial_json``,适配器侧知道这是残缺输入。
    """
    converted: list = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            converted.append({"type": "text", "text": block.get("text", "")})
        elif kind == "reasoning":
            converted.append({"type": "thinking", "text": block.get("text", "")})
        elif kind == "tool-call":
            arguments = block.get("arguments", "")
            try:
                parsed = json.loads(arguments) if arguments else {}
            except ValueError:
                parsed = {"_partial_json": arguments}
            converted.append({
                "type": "tool_use",
                "id": block.get("id"),
                "name": block.get("name", ""),
                "input": parsed,
            })
        elif kind == "tool-result":
            content = block.get("content") or []
            converted.append({
                "type": "tool_result",
                "tool_use_id": block.get("toolCallId"),
                "content": _convert_blocks(content),
                "is_error": block.get("isError", False),
            })
        # 未知块类型:不翻译(消费方按 ContentBlock 契约拒绝或忽略)
    return converted


def _convert_messages(messages: list) -> list:
    """把派生消息历史转换到 llm 契约(Message: role + content)。"""
    return [
        {"role": message.get("role", "user"), "content": _convert_blocks(message.get("content") or [])}
        for message in messages
    ]


def _to_llm_request(request: dict) -> LLMRequest:
    """请求折叠回 llm 契约:会话词表 → llm 词表,camelCase → snake_case。

    请求构建时保留会话形状(config + messages + system + tools +
    sessionId);这里转换成交付形状:stream 恒开、maxTokens →
    max_tokens、tools 的 parameters → input_schema。
    """
    tools = request.get("tools")
    llm_tools = None
    if tools:
        llm_tools = [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
            }
            for tool in tools
        ]
    payload: dict = {
        "messages": _convert_messages(request.get("messages") or []),
        "stream": True,
        "system": request.get("system"),
        "tools": llm_tools,
        "max_tokens": request.get("maxTokens"),
        "temperature": request.get("temperature"),
        "top_p": request.get("topP"),
        "stop_sequences": request.get("stop") or [],
    }
    # 省略缺省字段:None / 空工具 / 空 stop 都不进入交付形状
    return LLMRequest(**{k: v for k, v in payload.items() if v is not None and v != []})


class _RawChunkConverter:
    """统一流事件 → 原始块词表的转换器(只发 delta 的协议)。

    llm 客户端产出 unified StreamEvent(text_delta / thinking_delta /
    tool_use_start / tool_use_delta / usage / error / done);原始块
    词表(text-delta / reasoning-delta / tool-call-delta / usage /
    finish)是会话日志里 durable 的流事实。本转换器把流事件翻译
    成原始块并喂给 BlockAssembler —— 组装算法只有一个,重放保真
    与活流共用同一份。

    索引分配:三类块各自维护一个「当前块索引」,文本/推理增量
    追加到各自的当前块,工具调用每次 start 开新块;索引只在块内
    一致,不必连续(assembler 按到达序组装)。finish 事件把 done
    映射到 completed / max-tokens,error 映射到带 failure 的
    error 原因 —— 会话词表不落地这两者的 provider 专有细节。
    """

    def __init__(self) -> None:
        self._text_index: int | None = None
        self._thinking_index: int | None = None
        self._tool_index: int | None = None
        self._next_index = 0

    def _allocate(self) -> int:
        index = self._next_index
        self._next_index += 1
        return index

    def push(self, event) -> list:
        """把一个统一流事件转换成 0..N 个原始块并返回。"""
        kind = event.type
        if kind == "text_delta":
            if self._text_index is None:
                self._text_index = self._allocate()
            return [{"type": "text-delta", "index": self._text_index, "text": event.text or ""}]
        if kind == "thinking_delta":
            if self._thinking_index is None:
                self._thinking_index = self._allocate()
            return [{"type": "reasoning-delta", "index": self._thinking_index, "text": event.thinking or ""}]
        if kind == "tool_use_start":
            self._tool_index = self._allocate()
            return [{
                "type": "tool-call-delta",
                "index": self._tool_index,
                "id": event.tool_use_id,
                "name": event.tool_name,
            }]
        if kind == "tool_use_delta":
            if self._tool_index is None:
                self._tool_index = self._allocate()
            return [{
                "type": "tool-call-delta",
                "index": self._tool_index,
                "argumentsDelta": event.input_json_delta or "",
            }]
        if kind == "usage":
            return [{"type": "usage", "usage": event.usage.model_dump() if event.usage is not None else {}}]
        if kind == "error":
            return [{
                "type": "finish",
                "reason": {
                    "kind": "error",
                    "failure": {"message": event.error or "unknown error", "code": "UNKNOWN"},
                },
            }]
        if kind == "done":
            reason = (
                {"kind": "max-tokens"} if event.stop_reason == "length" else {"kind": "completed"}
            )
            return [{"type": "finish", "reason": reason}]
        return []  # 未知事件类型:不翻译


class _AgentInboxNotifications(InboxNotifications):
    """Inbox 变更 → agent 通知的适配器(构造一次,热路径免分配)。"""

    def __init__(self, dispatch) -> None:
        self._dispatch = dispatch

    def inserted(self, message: dict) -> None:
        self._dispatch.emit("agent/inbox/inserted", {"message": message})

    def discarded(self, message: dict) -> None:
        self._dispatch.emit("agent/inbox/discarded", {"message": message})

    def claimed(self, message: dict, turn: int) -> None:
        self._dispatch.emit("agent/inbox/claimed", {"message": message, "turn": turn})


class ReactLoopAgent:
    """驱动一个会话穿过回合与步骤边界的默认 driver。

    状态机:``phase`` 三态 —— idle(空闲,保留 lastTurn)/
    maintenance(维护任务排水中)/ running(驱动者排水中,保留
    turn/step 游标与 abort 控制器)。对外可见的 status 是 idle/
    running 两态;driver 的 async 活动经 ``activityDone`` future
    暴露,``when_idle()`` 等它停稳。
    """

    def __init__(self, loop_ctx, id_: str, options: dict, session) -> None:
        self._loop_ctx = loop_ctx
        self.id = id_
        self.options = options
        self.session = session
        # 融合派发器:构造一次,热路径免分配 —— 派发器持有 agent
        # 载体与注入规则,每次事件派发只做参数组装。
        self._dispatch = agent_events(loop_ctx, self)
        self._inbox = Inbox(session, _AgentInboxNotifications(self._dispatch))
        last_turn = 0
        for event in reversed(session.events):
            if event["type"] == "turn/start":
                last_turn = event["data"]["turn"]
                break
        self._phase: dict = {"kind": "idle", "last_turn": last_turn}
        # 「无活动」用 None 表示(没有已解决的 future 可等),
        # when_idle 先检查它再决定是否等待。
        self._activity_done: asyncio.Future | None = None
        self._scope = create_scope(loop_ctx, self)
        self.ctx = self._scope.ctx.extend({"agent": self})
        self._runtime_context = RuntimeContextProjection(self.ctx, session)
        # 本循环实例是否已把初始/恢复请求头折进日志
        self._request_header_logged = False

    # ---- 只读面 ----

    @property
    def status(self) -> str:
        return "idle" if self._phase["kind"] in ("idle", "maintenance") else "running"

    @property
    def inbox(self) -> Inbox:
        return self._inbox

    @property
    def scope(self):
        return self._scope

    # ---- 内部 ----

    def _set_phase(self, next_phase: dict) -> None:
        """提交相位并发布外部可见的状态翻转。"""
        previous_status = self.status
        self._phase = next_phase
        status = self.status
        if status != previous_status:
            self._dispatch.emit("agent/status", {"status": status})

    def _throw_error(self, error) -> None:
        """在它的活边界报告一次失败,然后抛出(驱动者包含它)。"""
        turn = self._phase["turn"] if self._phase["kind"] == "running" else self._phase["last_turn"]
        step = self._phase["step"] if self._phase["kind"] == "running" else 0
        self._dispatch.emit("agent/error", {"turn": turn, "step": step, "error": error})
        if isinstance(error, BaseException):
            raise error
        raise RuntimeError(str(error))

    # ---- Agent 协议 ----

    def send(self, message: dict, target: str, wakeup: bool) -> None:
        """把消息折进收件箱;wakeup 时唤醒驱动者。

        唤醒输入不能加入已中止的活动,所以它开下一个回合:分类
        在插入之前捕获 —— splice 观察者里的重入取消不能重新分类。
        """
        waking_after_abort = (
            wakeup and self._phase["kind"] != "idle" and self._phase["abort"].signal.aborted
        )
        resolved_target = "next-turn" if waking_after_abort else target
        self._inbox.append(resolved_target, message)
        if wakeup:
            self._wake_driver(waking_after_abort)

    def followup(self, message: dict) -> None:
        self.send(message, "next-turn", True)

    def steer(self, message: dict) -> None:
        self.send(message, "next-step", True)

    def inject(self, message: dict) -> None:
        self.send(message, "next-step", False)

    def cancel(self, cause, options: dict | None = None) -> None:
        """取消当前活动;keepInbox 保留排队输入与转向输入。"""
        options = options or {}
        if not options.get("keepInbox"):
            self._inbox.clear()
            if self._phase["kind"] != "idle":
                self._phase["wake_requested"] = False
        if self._phase["kind"] != "idle":
            self._phase["abort"].abort(cause)

    def run_maintenance(self, job):
        """在维护相位里运行一个任务:活跃驱动者存在时拒绝。

        维护任务在 idle 与 running 之间开一段排他区间;期间到达
        的唤醒被闩住,维护结束且队列仍有工作时重放。返回的协程
        await 维护完成。
        """
        if self._phase["kind"] != "idle":
            raise RuntimeError(f'agent "{self.id}" already has active work')
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        maintenance: dict = {
            "kind": "maintenance",
            "abort": AbortController(),
            "last_turn": self._phase["last_turn"],
            "wake_requested": False,
        }
        self._set_phase(maintenance)
        self._activity_done = done

        async def _run():
            try:
                return await job(maintenance["abort"].signal)
            finally:
                self._set_phase({"kind": "idle", "last_turn": maintenance["last_turn"]})
                if maintenance["wake_requested"] and self._inbox.has_pending:
                    self._wake_driver()
                if not done.done():
                    done.set_result(None)

        return _run()

    def when_idle(self):
        """等当前驱动者(或维护)排干;无活动时立即返回。"""
        async def _wait():
            while True:
                activity = self._activity_done
                if activity is None:
                    return
                await activity
                if activity is self._activity_done:
                    return
        return _wait()

    def _wake_driver(self, wake_after_abort: bool = False) -> None:
        """启动一个驱动者,或把唤醒闩在维护/已中止活动之后。

        空闲时的唤醒总是开它的回合边界,即使它的消息已被清除;
        只有闩住的重放在队列不再持有唤醒时被抑制。拆解(disposed)
        永不闩住 —— 拆解不等待任何模型回合。

        驱动者生命周期:driver future 代表「这次驱动已排干」,kick
        经 with_initiator 调度(发起者边界内运行)后,Task 的 done
        回调转发到 future —— 唤醒方 await when_idle 就等到驱动者
        停稳。无运行中事件循环时 with_initiator 返回裸协程,无法
        转发 —— 本方法要求运行中循环(harness 的常态),无循环的
        同步上下文里 send 需要调用方先建立循环。
        """
        if self._phase["kind"] != "idle":
            reason = self._phase["abort"].signal.reason
            if reason is None or reason.get("kind") != "disposed":
                if self._phase["kind"] == "maintenance" or wake_after_abort:
                    self._phase["wake_requested"] = True
            return
        loop = asyncio.get_running_loop()
        driver: asyncio.Future = loop.create_future()
        self._activity_done = driver
        self._set_phase({
            "kind": "running",
            "abort": AbortController(),
            "turn": self._phase["last_turn"],
            "step": 0,
            "wake_requested": False,
        })

        def _forward(task):
            if driver.done():
                return
            if task.cancelled():
                driver.cancel()
            else:
                error = task.exception()
                if error is not None:
                    driver.set_exception(error)
                else:
                    driver.set_result(None)

        # with_initiator 收 callable(其返回值可为协程):传裸方法而非
        # 已求值的协程 —— 调用发生在拷贝上下文内,协程在拷贝里创建,
        # 才能继承发起者归属(contextvars 在创建处捕获)。
        result = self._loop_ctx.agents.with_initiator(self, self._kick)
        if isinstance(result, asyncio.Task):
            result.add_done_callback(_forward)
        elif inspect.isawaitable(result):
            # 无运行中事件循环:with_initiator 无法调度,返回裸
            # 协程;这里也建不了 Task,驱动者无法启动(见 docstring)。
            raise RuntimeError("cannot wake an agent driver outside a running event loop")
        else:
            # kick 是 async 函数不会同步完成;防御性直接解决 driver
            if not driver.done():
                driver.set_result(None)

    async def _kick(self) -> None:
        """驱动者主循环:连续回合直到队列空或失败。

        已报告的失败与取消在驱动者边界被包含(不逃逸到调用方);
        驱动者退出时把自己的 running 相位交还 idle,并重放闩住的
        唤醒(队列仍持有工作)。
        """
        try:
            while await self._turn():
                pass
        except Exception:  # noqa: BLE001 -- 驱动者边界包含一切失败
            pass
        finally:
            if self._phase["kind"] == "running":
                turn = self._phase["turn"]
                wake_requested = self._phase["wake_requested"]
                self._set_phase({"kind": "idle", "last_turn": turn})
                if wake_requested and self._inbox.has_pending:
                    self._wake_driver()

    async def _pre_step(self, target: str, position: dict) -> dict:
        """提议一个步骤:认领输入、装配提示、投影运行时上下文。

        @returns {'kind': 'reject'} 或 {'kind': 'enter', 'messages',
          'assembly'} —— reject 由监听者否决(回合以 blocked 结局);
          enter 的 messages 是进入步骤的输入批次(context 快照被
          折进批次尾,若投影产出了候选)。
        """
        if self._phase["kind"] != "running":
            raise RuntimeError(f'agent "{self.id}": pre-step outside running phase')
        signal = self._phase["abort"].signal
        claimed = self._inbox.claim(target, position["turn"])
        system_prompt = getattr(self._loop_ctx, "systemPrompt", None)
        if system_prompt is None:
            raise RuntimeError(
                f'agent "{self.id}": systemPrompt service is not registered'
            )
        assembly = await system_prompt.assemble(assemble_context_for(self, signal))
        signal.throw_if_aborted()
        sections = render_context_sections(assembly)
        context = self._runtime_context.project(join_context_sections(sections), sections)
        # waterfall 最内层 next:返回默认决策(协程,经 _resolve_waterfall 归一)
        default = lambda: _enter_decision(claimed, context)  # noqa: E731
        result = self._dispatch.waterfall(
            "agent/pre-step",
            {"messages": claimed, **position, "signal": signal},
            default,
        )
        decision = await _resolve_waterfall(result)
        signal.throw_if_aborted()
        if decision["kind"] == "reject":
            return decision
        return {**decision, "assembly": assembly}

    async def _turn(self) -> bool:
        """开一个回合并排干它的步骤,直到回合结局或队列空。

        @returns 队列是否仍有工作(驱动者决定是否继续下一回合)。
        """
        if self._phase["kind"] != "running":
            self._throw_error(RuntimeError(f'agent "{self.id}": turn without driver reservation'))
        phase = self._phase
        signal = phase["abort"].signal
        signal.throw_if_aborted()
        turn = phase["turn"] + 1
        try:
            self.session.append("turn/start", {"turn": turn})
        except Exception as error:  # noqa: BLE001 -- 边界事件失败按报告路径走
            self._throw_error(error)
        phase["turn"] = turn
        turn_ends: dict | None = None
        target = "next-turn"
        try:
            while True:
                signal.throw_if_aborted()
                step = phase["step"] + 1
                decision = await self._pre_step(target, {"turn": turn, "step": step})
                if decision["kind"] == "reject":
                    turn_ends = {"kind": "blocked"}
                    return False
                if turn_ends and len(decision["messages"]) == 0:
                    break
                # 被移除的唤醒消息或重写为空的 enter 仍拥有初始回合
                # 边界,但花不了一次模型调用。
                if phase["step"] == 0 and len(decision["messages"]) == 0:
                    turn_ends = {"kind": "completed"}
                    return False
                signal.throw_if_aborted()
                self.session.append("step/start", {"turn": turn, "step": step})
                phase["step"] = step
                try:
                    for message in decision["messages"]:
                        self.session.append("user/message", message, surface_op="append")
                    step_end = await self._step(decision["assembly"])
                    # max-tokens 是粘性的:一旦任何步骤撞到上限,之后
                    # 正常完成的步骤不得降级回合结局。
                    if turn_ends is None or turn_ends["kind"] != "max-tokens":
                        turn_ends = step_end
                finally:
                    self.session.append("step/end", {"turn": turn, "step": step})
                signal.throw_if_aborted()
                if turn_ends and len(self._inbox.next_step) == 0:
                    await self._dispatch.serial("agent/turn-stopping", {"turn": turn, "signal": signal})
                    signal.throw_if_aborted()
                if turn_ends and len(self._inbox.next_step) == 0:
                    break
                target = "next-step"
        except Exception as error:  # noqa: BLE001 -- 见下面的分类
            if signal.aborted:
                turn_ends = {"kind": "aborted", "reason": signal.reason}
                raise
            # 每个失败都是结构化的:LLMError 保留它的事实,其余压扁
            # 成 errorChain 文本的 UNKNOWN 码。
            failure = getattr(error, "failure", None)
            turn_ends = {
                "kind": "error",
                "error": failure if failure is not None
                else {"message": error_chain(error), "code": "UNKNOWN"},
            }
            self._throw_error(error)
        finally:
            try:
                self.session.append("turn/end", {"turn": turn, "reason": turn_ends})
            except Exception as error:  # noqa: BLE001 -- 结局事件失败按报告路径走
                self._throw_error(error)
        if not self._inbox.has_pending:
            return False
        phase["abort"] = AbortController()
        # 新控制器使旧控制器上的闩失效:活驱动者自己认领队列
        phase["wake_requested"] = False
        phase["step"] = 0
        return True

    async def _step(self, assembly: dict):
        """执行一个步骤:请求 → 流组装 → 工具执行,直到完成或 max-tokens。

        @returns {'kind': 'completed'|'max-tokens'} 或 None —— None
          表示本轮工具执行开了新输入(消息进 next-step,回合继续)。
        """
        if self._phase["kind"] != "running":
            raise RuntimeError(f'agent "{self.id}": step outside running phase')
        phase = self._phase
        signal = phase["abort"].signal
        turn = phase["turn"]
        step = phase["step"]
        signal.throw_if_aborted()
        system = render_prompt(assembly)

        while True:
            request = await self._build_request(
                turn, step, assembly.get("tools") or [], system,
                self.session.derive_messages(), signal,
            )
            assembler = BlockAssembler()
            converter = _RawChunkConverter()
            chunk_seqs: list[int] = []
            try:
                llm = getattr(self._loop_ctx, "llm", None)
                if llm is None:
                    raise RuntimeError(f'agent "{self.id}": llm service is not registered')
                stream = llm.stream(
                    _to_llm_request(request),
                    model=f"{request['provider']}:{request['model']}",
                )
                signal.throw_if_aborted()
                async for event in stream:
                    signal.throw_if_aborted()
                    for chunk in converter.push(event):
                        chunk_seqs.append(
                            self.session.append(
                                "assistant/chunk", {"turn": turn, "step": step, "chunk": chunk}
                            )["seq"]
                        )
                        assembler.push(chunk)
                signal.throw_if_aborted()
            except Exception as error:  # noqa: BLE001 -- 见下面的分类
                if signal.aborted:
                    content = assembler.interrupted_blocks()
                    if len(content) > 0:
                        message = create_assistant_message({
                            "content": content,
                            "source": {"provider": request["provider"], "model": request["model"]},
                        })
                        payload: dict = {
                            "turn": turn, "step": step, "message": message, "interrupted": True,
                        }
                        if assembler.usage is not None:
                            payload["usage"] = assembler.usage
                        self.session.append(
                            "assistant/message", payload,
                            surface_op="append", source_event_seqs=chunk_seqs,
                        )
                raise
            finish = assembler.finish
            if finish["kind"] in ("error", "aborted"):
                result = self._dispatch.waterfall(
                    "agent/request-error",
                    {
                        "turn": turn, "step": step, "provider": request["provider"],
                        "failure": finish["failure"], "retryPolicy": None, "signal": signal,
                    },
                    lambda: None,
                )
                action = await _resolve_waterfall(result)
                signal.throw_if_aborted()
                if action != "retry":
                    error = LLMError(finish["failure"]["message"])
                    # 回合结局的分类需要保留失败的结构化事实(message
                    # + code),而 llm 包的 LLMError 只携带 message:
                    # 抛出前把 failure 动态挂到异常上,turn() 的分类
                    # 用 getattr 读取,拿不到时按 UNKNOWN 压扁。
                    error.failure = finish["failure"]  # type: ignore[attr-defined]
                    raise error
                continue

            replay_state = assembler.replay_state
            source: dict = {"provider": request["provider"], "model": request["model"]}
            if replay_state is not None:
                source["replayState"] = replay_state
            message = create_assistant_message({
                "content": assembler.blocks(),
                "source": source,
            })
            payload = {"turn": turn, "step": step, "message": message}
            if assembler.usage is not None:
                payload["usage"] = assembler.usage
            self.session.append(
                "assistant/message", payload,
                surface_op="append", source_event_seqs=chunk_seqs,
            )
            if finish["kind"] == "max-tokens":
                return {"kind": "max-tokens"}

            tool_calls = [b for b in message["content"] if b["type"] == "tool-call"]
            if len(tool_calls) == 0:
                return {"kind": "completed"}
            outcome = await execute_tool_calls(
                self._loop_ctx, turn, step, tool_calls, signal,
                lambda context: self._inbox.splice(
                    "next-step", len(self._inbox.next_step), 0, [context]
                ),
            )
            # 执行结局是 dict(concluded/aborted 两键):取真值键判定,
            # 不是把整个 dict 当布尔 —— 空注册表的失败结果也要继续
            # 下一轮给模型看,而非误判为回合终局。
            return {"kind": "completed"} if outcome["concluded"] else None

    async def _build_request(self, turn: int, step: int, tools: list, system: str,
                             boundary_messages: list, signal: AbortSignal) -> dict:
        """组合一份请求,绑定到它解析出的提供者/模型配置。

        @returns 会话词表的请求 dict(未转换 —— 交付转换在 _step
          的 stream 调用边界)。
        """
        session = self.session
        # 循环实例从它声明的路由开始,只恢复那个精确模型拥有的
        # 显式 effort;后续步骤重新决议被标记的适配器默认值。
        persisted_header = session.request_header()
        persisted_config = persisted_header.get("config") if persisted_header is not None else None
        route = {"provider": self.options.get("provider") or "", "model": self.options.get("model") or ""}
        reasoning_effort = None
        if persisted_config is not None:
            if (
                persisted_config.get("provider") == route["provider"]
                and persisted_config.get("model") == route["model"]
                and (persisted_header.get("adapterDefaults") or {}).get("reasoningEffort") is not True
            ):
                reasoning_effort = persisted_config.get("reasoningEffort")
        max_tokens = self.options.get("maxTokens")
        if self._request_header_logged:
            # 实例已折叠过头:折叠当前持久头(去掉适配器派生值);
            # logged 不变量保证持久头存在,防御分支只是类型守卫
            seed_config = (
                _request_proposal(persisted_header) if persisted_header is not None else {**route}
            )
        else:
            seed_config = {**route}
            if reasoning_effort is not None:
                seed_config["reasoningEffort"] = reasoning_effort
            if max_tokens is not None:
                seed_config["maxTokens"] = max_tokens
        result = self._dispatch.waterfall(
            "agent/request",
            {"turn": turn, "step": step, "signal": signal},
            lambda: seed_config,
        )
        proposed_config = await _resolve_waterfall(result)
        signal.throw_if_aborted()
        if not proposed_config.get("provider") or not proposed_config.get("model"):
            raise RuntimeError(
                f'agent "{self.id}" has no provider/model: set AgentOptions.provider and '
                "AgentOptions.model or supply both via the agent/request waterfall"
            )
        # 完整的适配器解析(prepareCall:按精确模型补默认值、算
        # 上下文窗口)尚未落地;当前配置即最终配置,adapterDefaults
        # / contextWindow 均为空,后续批次在 llm 侧补上后这里只需
        # 接住 preparedCall 的两个派生字段。
        config = proposed_config

        header = canonical_header({
            "config": config,
            **({"system": system} if system else {}),
            **({"tools": tools} if len(tools) > 0 else {}),
        })
        baseline = session.request_header()
        if not self._request_header_logged:
            session.append("request/header", {
                "header": header,
                "reason": "initial" if baseline is None else "resume",
            })
            self._request_header_logged = True
        elif baseline is None or not header_equals(baseline, header):
            session.append("request/header", {"header": header, "reason": "change"})

        # contextWindow 来自适配器解析(此处恒无);请求上下文只记
        # 变化 —— 路由不变就不写日志。
        request_context = {"provider": config["provider"], "model": config["model"]}
        previous_context = session.request_context()
        if (
            previous_context is None
            or previous_context.get("provider") != request_context["provider"]
            or previous_context.get("model") != request_context["model"]
        ):
            session.append("request/context", request_context)
        signal.throw_if_aborted()

        request = {
            **config,
            "messages": boundary_messages,
            **({"system": header["system"]} if header.get("system") is not None else {}),
            **({"tools": header["tools"]} if header.get("tools") is not None else {}),
            "sessionId": session.id,
        }
        # 取消不放进请求体:客户端流没有 per-request cancel,取消
        # 在 step 循环的 ``signal.throw_if_aborted()`` 检查点生效。
        # 标记让客户端事件流能认出本请求属于 agent 循环(重放锚点)。
        mark_agent_loop_request(request)
        return request


async def _enter_decision(claimed: list, context: dict | None) -> dict:
    """pre-step 的默认决策:进入步骤,上下文快照折进批次尾。"""
    messages = claimed if context is None else [*claimed, context]
    return {"kind": "enter", "messages": messages}
