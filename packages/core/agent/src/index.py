"""agent 服务:活注册表、工厂委派、进程内 initiator 作用域
(参考实现 agent/index.ts 实现)。

具体创建与驱动属于循环(agent-loop);本模块是服务面:

- ``AgentRegistry``(ctx.agents):跟踪活 agent,携带发起 agent
  穿过一条进程内异步驱动链(initiator 作用域);
- 创建工厂按 AgentFactory 协议注册,create/resume 经它委派;
- agent 生命周期四步(enter → announce / detach),与 SessionStore
  同构:同步抛错的创建监听者否决发布并回滚附件。

**Python 实现差异**(均在注释中标出):

- AsyncLocalStorage ×2 → contextvars.ContextVar ×2(嵌套链用
  parent 指针,继承语义一致:coroutine 捕获创建处上下文);
- typert 注册(参考实现经 ctx.inject(['typert']))→ 砍掉:批次 2 无
  typert 服务,查找面无人消费,留可扩展位;
- ``internal/status`` 事件源(参考实现 在服务 fiber 开始卸载时提前
  close initiators)→ 无该事件,close/dispose 都由构造时注册的
  fiber effect 卸载 disposer 承担(效应时序略晚,语义一致);
- getTraceable 服务追踪重定向 → 直接调用工厂(注释注明)。
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from typing import Callable

from cordis import Service

from .dispatch import agent_carrier
from .invariant import AgentStatusInvariant

__all__ = [
    "AgentFactory",
    "AgentHandle",
    "AgentRegistry",
    "AgentSetup",
    "AgentSetupCommit",
    "CreateAgentOptions",
    "ResumeAgentOptions",
]

#: create/resume 在工厂注册前被调用的错误
NO_FACTORY_MESSAGE = "no agent factory registered (load an agent-loop plugin)"
#: 无活动 initiator 边界时 require 的错误
NO_INITIATOR_MESSAGE = "no initiating agent is active"
#: initiator 作用域已关闭/销毁后的错误
DISPOSED_INITIATOR_MESSAGE = "agent initiator scope is disposed"


class AgentSetupCommit:
    """未发布 Agent 设置的同步终结器:在精确的发布提交点校验。"""

    def commit(self) -> None:  # pragma: no cover -- 协议占位
        """校验并提交;发布必须回滚未发布 Agent 时抛错。"""


#: 设置回调: (agent_ctx) -> AgentSetupCommit | Promise | None;见 create 文档
AgentSetup = Callable

#: 程序化创建选项(参考实现 CreateAgentOptions):sessionId + meta 创建
#: 元数据(cwd/parentSession/seedLength/origin/delegationDepth/
#: agentPreset)+ seed 重放前缀 + agentOptions + signal + setup。
CreateAgentOptions = dict

#: 恢复选项(参考实现 ResumeAgentOptions):resumeSessionId + agentOptions
#: + signal + setup。
ResumeAgentOptions = dict


class AgentFactory:
    """agent 创建工厂(循环实现经 set_factory 提供给注册表)。

    契约保留在 agent 接口上,消费者(如 ACP 桥)只对着
    ctx.agents 编程,不依赖具体的 agent-loop 实现包。
    """

    def create_agent(self, owner_ctx, options: dict):
        """在调用方提供的会话 id 上创建新 agent(异步)。"""

    def resume(self, owner_ctx, options: dict):
        """准备持久化会话并在其上恢复 agent(异步)。"""


class AgentHandle:
    """属主 agent + 其 disposer:持有者才拥有拆解能力。"""

    def __init__(self, agent, dispose) -> None:
        self.agent = agent
        self._dispose = dispose

    async def dispose(self) -> None:
        await self._dispose()


class AgentEntry:
    """一个精确注册表条目的全部可变生命周期状态。"""

    def __init__(self, id_: str, agent, owner, carrier) -> None:
        self.id = id_
        self.agent = agent
        self.owner = owner  # 运行时创建者 agent;独立于耐久会话血缘
        self.carrier = carrier
        self.announced = False
        self.announcing = False
        self.detach_requested = False


class InitiatorRun:
    """一个被跟踪的边界 + 其继承嵌套链。"""

    def __init__(self, active: bool, parent) -> None:
        self.active = active
        self.parent = parent


class AgentRegistry(Service):
    """agent 服务(ctx.agents):跟踪活 agent,携带发起 agent 穿过
    一条进程内异步驱动链。

    agent 创建由实现 AgentFactory 的插件(agent-loop)提供,经
    set_factory 注册。initiator 方法只做同进程因果归属:环境在场
    既不是活性证明也不是授权 —— 主体与属主保持显式,worker /
    进程 / 持久化 / 线上边界的身份同样显式。返回的 Promise 边界
    在拆解期间排干;启动属主 fiber 卸载的嵌套血缘除外。
    """

    provide = "agents"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.store: dict[str, AgentEntry] = {}
        self.factory: dict | None = None
        #: AsyncLocalStorage → contextvars:发起 agent 与嵌套链
        self._initiators: contextvars.ContextVar = contextvars.ContextVar("agents.initiators", default=None)
        self._initiator_runs: contextvars.ContextVar = contextvars.ContextVar("agents.initiatorRuns", default=None)
        self._initiator_state: str = "active"
        self._active_initiator_runs = 0
        self._initiator_drain: asyncio.Future | None = None
        self._initiator_disposal: asyncio.Future | None = None
        self._invariant = AgentStatusInvariant()
        # ctx.agent DX 访问器:普通上下文读 None(同款设计;每个
        # Agent.ctx 用 own property 阴影它,own 属性先于代理解析)。
        ctx.accessor("agent", {"get": lambda ctx, _: None})
        # agent/status 不变式:no-op 状态转换是缺陷(参考实现经
        # invariants 服务 fail;内部化为全局监听 + 抛错)。
        ctx.on("agent/status", self._on_agent_status, {"global": True})
        # 服务 fiber 卸载时:先拒绝新边界,再排干继承延续,最后
        # 标记销毁(generator effect 逆序执行 yield 项)。
        self.ctx.fiber.effect(self._initiator_lifecycle, "agents.initiatorLifecycle()")

    # ---- initiator 作用域 ----

    def _initiator_lifecycle(self):
        yield self.dispose_initiators
        yield self.close_initiators

    def _on_agent_status(self, payload: dict) -> None:
        self._invariant.record(payload["agent"].id, payload["status"])

    def current_initiator(self):
        """读继承的发起 agent;边界外与显式清除边界内为 None。

        供日志/追踪/指标/主机归属用;父创建子时,setup 报告因果
        父,而 agentCtx.agent 识别子。
        """
        self._assert_initiators_readable()
        return self._initiators.get()

    def require_initiator(self):
        """读发起 agent;无活动边界时抛错。"""
        agent = self.current_initiator()
        if agent is None:
            raise RuntimeError(NO_INITIATOR_MESSAGE)
        return agent

    def with_initiator(self, agent, operation):
        """以精确的一个 agent 为进程内发起者运行操作。

        操作返回的精确值(同步或协程)被保留。队列或线上接收方
        只有在校验显式身份并解析出精确活 agent 后才能建立该
        边界 —— 本方法两者都不做。
        """
        return self._run_with_initiator(agent, operation)

    def without_initiator(self, operation):
        """在隐藏任何继承发起 agent 的边界内运行操作。

        供惰性共享定时器/队列泵/池维护/观察者用,使它们不继承
        恰好初始化它们的第一个 agent。只清除 initiator 归属,
        不动显式字段。
        """
        return self._run_with_initiator(None, operation)

    def _run_with_initiator(self, agent, operation):
        if self._initiator_state != "active":
            raise RuntimeError(DISPOSED_INITIATOR_MESSAGE)
        run = InitiatorRun(active=True, parent=self._initiator_runs.get())
        self._active_initiator_runs += 1
        # contextvars:在拷贝上下文里设两个 var,再运行操作 ——
        # 操作内创建的任务继承该拷贝(coroutine 捕获创建处上下文)。
        context = contextvars.copy_context()
        context.run(lambda: self._initiators.set(agent))
        context.run(lambda: self._initiator_runs.set(run))
        try:
            result = context.run(operation)
        except BaseException:
            self._release_initiator_run(run)
            raise
        if inspect.isawaitable(result):
            try:
                # Python 3.13 的 Task 捕获创建时所在线程的上下文
                # (不是 coroutine 的 cr_context):必须在拷贝内
                # ensure_future,任务才继承拷贝里的 initiator。
                # 返回值身份从精确 coroutine 变为已调度 task ——
                # await 语义等价,但上下文保留。
                task = context.run(lambda: asyncio.ensure_future(result))
                task.add_done_callback(lambda _: self._release_initiator_run(run))
                return task
            except RuntimeError:
                # 无运行中事件循环:同步上下文无法调度,直接释放
                self._release_initiator_run(run)
                return result
        self._release_initiator_run(run)
        return result

    def close_initiators(self) -> None:
        """拒绝新 initiator 边界,让继承的延续排干。"""
        if self._initiator_state == "active":
            self._initiator_state = "closing"

    def dispose_initiators(self):
        """等返回的 Promise 边界,然后使保留引用失效(惰性单发)。

        返回协程,由 effect 拆解 await(单发:只 await 一次)。
        """
        if self._initiator_disposal is None:
            self._initiator_disposal = self._dispose_initiators_async()
        return self._initiator_disposal

    async def _dispose_initiators_async(self) -> None:
        self.close_initiators()
        self._release_reentrant_initiator_runs()
        if self._active_initiator_runs != 0:
            self._initiator_drain = asyncio.get_running_loop().create_future()
            await self._initiator_drain
        self._initiator_state = "disposed"
        # ContextVar 无 disable;disposed 态由 _assert 守卫,读回 None
        self._initiators.set(None)
        self._initiator_runs.set(None)

    def _release_reentrant_initiator_runs(self) -> None:
        """排除发起本拆解的边界链自身。"""
        run = self._initiator_runs.get()
        while run is not None:
            self._release_initiator_run(run)
            run = run.parent

    def _release_initiator_run(self, run: InitiatorRun) -> None:
        if not run.active:
            return
        run.active = False
        self._active_initiator_runs -= 1
        if self._active_initiator_runs != 0:
            return
        if self._initiator_drain is not None:
            self._initiator_drain.set_result(None)
            self._initiator_drain = None

    def _assert_initiators_readable(self) -> None:
        if self._initiator_state == "disposed":
            raise RuntimeError(DISPOSED_INITIATOR_MESSAGE)

    # ---- 工厂 ----

    def set_factory(self, factory: AgentFactory):
        """注册 agent 创建工厂(循环构造时调用,effect 作用域)。

        已注册时抛错;返回的 disposer 清空工厂槽。参考实现经
        getTraceable 重定向调用到调用方上下文;Python 直接持有
        工厂对象,调用在 create/resume 时显式传 owner_ctx。
        """
        def _install():
            if self.factory is not None:
                raise RuntimeError("an agent factory is already registered")
            self.factory = {"target": factory}

            def _dispose():
                self.factory = None

            return _dispose

        return self.ctx.fiber.effect(_install, "agents.setFactory()")

    def _require_factory(self) -> dict:
        if self.factory is None:
            raise RuntimeError(NO_FACTORY_MESSAGE)
        return self.factory

    # ---- 创建面 ----

    async def create(self, options: dict) -> AgentHandle:
        """经注册工厂创建并发布新 agent。

        与 register(记录已构造的 agent)不同:这里构造 agent 及
        其会话。工厂未注册或创建/setup 失败时拒绝。
        """
        owner_ctx = self.ctx
        target = self._require_factory()["target"]
        # 参考实现:Reflect.apply(target.createAgent, receiver, [ownerCtx, options])
        # —— receiver 是 caller 追踪的上下文代理;Python 直接调用。
        return await target.create_agent(owner_ctx, options)

    async def resume(self, options: dict) -> AgentHandle:
        """载入持久化会话并经注册工厂在其上恢复 agent。"""
        owner_ctx = self.ctx
        target = self._require_factory()["target"]
        return await target.resume(owner_ctx, options)

    # ---- 注册面 ----

    def register(self, agent) -> object:
        """注册一个活 agent;同 id 已注册则抛错。

        agent/created 于注册时发出,agent/disposed 于调用方 fiber
        卸载时发出 —— 都以 agent 自己的作用域载体派发,与哪个
        上下文调用 register 无关。返回的 disposer 是 EXACT effect
        disposer:身份敏感 —— 持有拆解顺序的复合(generator)
        effect 必须 yield 本函数,Cordis 才会在那一 yield 位置
        嵌套注销(包装一层会让它在属主卸载时成为并发兄弟,
        agent/disposed 会抢在最终回合排干前发出)。
        """
        def _effect():
            yield self.enter(agent, self.ctx.agent)
            self.announce(agent)

        return self.ctx.fiber.effect(_effect, "agents.register()")

    def enter(self, agent, owner) -> object:
        """插入一个已构造的 agent 而不宣布。

        异步 agent 工厂的高级有序生命周期原语:先在未发布状态下
        完成 setup,再把返回的 detach 闭包折进其预装的复合
        拆解,最后 announce。普通调用方用 register。返回幂等
        detach;从同步 agent/created 监听者里调用时,移除与销毁
        等到那次创建派发退栈。
        """
        id_ = agent.id
        if id_ != agent.session.id:
            raise RuntimeError(f'agent id "{id_}" does not match session id "{agent.session.id}"')
        carrier = agent_carrier(agent)
        # 权威碰撞边界:并发 create/resume 都可能 prepare,只有
        # 一个精确条目能发布。
        if id_ in self.store:
            raise RuntimeError(f'agent "{id_}" is already registered')
        entry = AgentEntry(id_, agent, owner, carrier)
        self.store[id_] = entry
        entered = True

        def detach():
            nonlocal entered
            if not entered:
                return
            entered = False
            # 每次创建派发到达的监听者必须观察同一活条目,销毁
            # 必须跟随创建:监听者可能持有高级 detach 能力 ——
            # 可见性与配对销毁都推迟到 announce() 的同步派发退栈。
            if entry.announcing:
                entry.detach_requested = True
                return
            self._detach_entered(entry)

        return detach

    def _detach_entered(self, entry: AgentEntry) -> None:
        """移除一个精确的已进入 agent,已宣布时发出配对销毁边。"""
        entry.detach_requested = False
        # 过期能力不得删除后续同 id 生命周期(enter 在一次性
        # detach 能力存活期间拒绝替换,这里兜底)。
        if self.store.get(entry.id) is not entry:
            return
        del self.store[entry.id]
        self._invariant.forget(entry.id)
        # 宣布前回滚的插入从未外部创建,发 disposed 会发明
        # 不可能的生命周期边。标记在 created 发出之前:更晚的
        # created 监听者抛错时,更早的已观察到它,必须见销毁。
        if not entry.announced:
            return
        self._emit_disposed(entry)

    def _emit_disposed(self, entry: AgentEntry) -> None:
        """经条目稳定载体发出配对销毁边,单监听者错误包含化。"""
        args = [entry.carrier, "agent/disposed", {"agent": entry.agent}]
        callbacks = self.ctx.events.dispatch("emit", args)
        for callback in callbacks:
            try:
                returned = callback(*args)
                if inspect.isawaitable(returned):
                    self._observe_disposed_rejection(returned, entry.id)
            except Exception as error:  # noqa: BLE001 -- 通知包含化
                self.ctx.logger.warn(f'agent "{entry.id}": agent/disposed listener threw: {error}')

    def _observe_disposed_rejection(self, awaitable, id_: str) -> None:
        try:
            task = asyncio.ensure_future(awaitable)
        except RuntimeError:
            return
        task.add_done_callback(
            lambda t: t.exception()
            and self.ctx.logger.warn(f'agent "{id_}": agent/disposed listener rejected: {t.exception()}')
        )

    def announce(self, agent) -> None:
        """对先前 enter 的 agent 发出恰好一次 agent/created。

        非该 id 的精确活条目或已宣布(含创建监听者里的重入)抛错。
        标记先于派发:监听者不能递归创建第二条生命周期边;
        detach 仍把部分送达的首条边配对。
        """
        entry = self.store.get(agent.id)
        if entry is None or entry.agent is not agent:
            raise RuntimeError(f'agent "{agent.id}" is not live in this registry')
        if entry.announced or entry.announcing:
            raise RuntimeError(f'agent "{entry.id}" was already announced')
        entry.announcing = True
        entry.announced = True
        args = [entry.carrier, "agent/created", {"agent": entry.agent}]
        try:
            callbacks = self.ctx.events.dispatch("emit", args)
            for callback in callbacks:
                # 同步失败否决发布并回滚;返回 Promise 的拒绝发生
                # 在同步边界之后,观察并报告而不是泄漏未处理拒绝。
                returned = callback(*args)
                if inspect.isawaitable(returned):
                    self._observe_disposed_rejection(returned, entry.id)
        finally:
            entry.announcing = False
            if entry.detach_requested:
                self._detach_entered(entry)

    # ---- 查询面 ----

    def get(self, id_: str):
        """查活 agent;无该 id 的活 agent 时返回 None。"""
        entry = self.store.get(id_)
        return entry.agent if entry is not None else None

    def is_owned_by(self, id_: str, owner) -> bool:
        """活 agent 是否恰由一个父 agent 的作用域上下文创建。

        运行时属主独立于耐久会话血缘,无关提供者复用 id 时仍
        无歧义。
        """
        entry = self.store.get(id_)
        return entry is not None and entry.owner is owner

    def list(self) -> list:
        """全部活 agent,按注册序;返回新数组,改动不影响注册表。"""
        return [entry.agent for entry in self.store.values()]

    def roots(self) -> list:
        """全部活顶层 agent,按注册序(无属主上下文创建的)。"""
        return [entry.agent for entry in self.store.values() if entry.owner is None]
