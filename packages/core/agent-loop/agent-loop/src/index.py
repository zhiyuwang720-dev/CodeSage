"""具体 agent 循环服务:创建有作用域的 ReactLoopAgent,经
agent/session 注册表发布,并拥有它们的有序拆解。

本模块是工厂与服务面(agent-loop 插件的入口):把配置与运行时
服务(会话、agent 注册表、llm、工具、系统提示)编排成 agent 的
生命周期。一个 agent 的生命周期经过「准备 → 发布 → 活跃 →
拆解」四个相位,与操作系统进程的生命周期同构:准备期持有未
公布资源(可回滚),发布即注册进可见性面,拆解是注册的逆序
回滚 —— 且拆解由**多属主**共享(调用方取消、属主 fiber 卸载、
工厂拆除),任一属主触发,全部 await 同一份静默(记忆化
disposer:类比进程回收的引用计数 —— 谁最后离开谁执行清理,
多次触发只执行一次)。

**实现差异**(均在注释中标出):

- settings/schemastery 配置面省略(批次 3 引入):maxParallelToolCalls
  在构造时一次性解析,不响应运行期变更;
- ``Promise.withResolvers`` → asyncio.Future/Event;fire-and-forget
  的 promise 链 → ensure_future 包裹的 task(所有创建点都要求
  运行中的事件循环,与 cordis 插件加载上下文一致);
- ``using`` 析构 → SessionPreparation.dispose() 显式调用(幂等)。
"""

from __future__ import annotations

import asyncio
import inspect
import uuid

from cordis.fiber import FiberState
from cordis.service import Service

from llm.llm.src.error_chain import error_chain

from core.agent.src.dispatch import emit_agent_event
from core.agent.src.index import AgentFactory, AgentHandle, AgentSetupCommit
from core.session.src.preparation import SessionPreparation
from core.session.src.types import SessionId

from .abort import AbortController, any_signals
from .agent import ReactLoopAgent
from .constants import DEFAULT_MAX_PARALLEL_TOOL_CALLS

__all__ = [
    "AGENT_LOOP_SETTINGS_NAMESPACE",
    "CONFIGURED_AGENT_IDENTITIES_KEY",
    "DEFAULT_MAX_PARALLEL_TOOL_CALLS",
    "AgentLoop",
]

#: 生命周期状态:只有活跃纤维能拥有或服务一个新的 agent 生命周期。
#: 类比操作系统的进程状态:UNLOADING/DISPOSED/FAILED 的进程不会
#: 再接受新工作,等待中的创建请求必须放弃而不是悬挂。
INACTIVE_STATES = frozenset({
    FiberState.UNLOADING,
    FiberState.DISPOSED,
    FiberState.FAILED,
})

#: 启动器(launcher)在 Loader 挂载前设置上下文键,指定配置 agent
#: 的精确会话身份;launcher 拥有身份,因为只有它知道会话是否已
#: 物化 —— 配置行只保留模型路由(普通可改配置)。
CONFIGURED_AGENT_IDENTITIES_KEY = "configuredAgentIdentities"


class FactoryOwnership:
    """工厂级所有权:活 agent 的拆解 + 配置启动工作。

    工厂拆除与 fiber 卸载是两件事:任何一者发生,工厂都不再接受
    新生命周期,并等待已接受的(活 agent 拆解、配置启动任务)
    全部结算。类比进程组的回收:组长退出时,组内所有子进程的
    清理必须完成,新 fork 被拒绝。
    """

    def __init__(self, fiber) -> None:
        self._fiber = fiber
        self._accepting = True
        self._teardown = AbortController()
        #: 拆除开始的信号:waitWhileActive 与它竞速,结束等待
        self._inactive = asyncio.Event()
        #: 活 agent 的共享拆解(记忆化 disposer)
        self._live_agents: set = set()
        #: 配置启动任务(restore/create 等)
        self._startup_tasks: set[asyncio.Future] = set()

    @property
    def signal(self):
        """工厂拆除开始时 abort(原因:agent loop 不活跃错误)。"""
        return self._teardown.signal

    def is_active(self) -> bool:
        return self._accepting and self._fiber.state not in INACTIVE_STATES

    def track(self, dispose):
        """跟踪一个活 agent 的共享拆解,直到它执行过。"""
        self._live_agents.add(dispose)
        return lambda: self._live_agents.discard(dispose)

    def track_startup(self, job) -> asyncio.Future:
        """加入 agent 存在之前开始的配置启动工作;返回调度后的 task。

        同步调度(fire-and-forget):调用方在运行中的事件循环里。
        """
        future = asyncio.ensure_future(job)
        self._startup_tasks.add(future)
        future.add_done_callback(self._startup_tasks.discard)
        return future

    def track_wrapper(self, job) -> asyncio.Future:
        """加入一个公共 create/resume 延续;工厂拆除 await 它的结算。"""
        return self.track_startup(self._swallow(job))

    async def _swallow(self, job) -> None:
        """fire-and-forget 包装:失败已被调用方经 report 消费,此处吞掉。"""
        try:
            await job
        except BaseException:
            pass

    async def wait_while_active(self, job) -> None:
        """等待 task 结算;工厂拆除开始时提前返回(不再值得等)。"""
        await asyncio.wait(
            {job, self._inactive.wait()},
            return_when=asyncio.FIRST_COMPLETED,
        )

    async def dispose(self) -> None:
        """停止接受、abort 在途等待,并行结算全部活拆解与启动任务。

        一项清理失败不拖垮其余(return_exceptions):回收是尽力而为,
        单个 agent 的拆解失败不该阻止其它 agent 收尾。
        """
        self._accepting = False
        self._teardown.abort(RuntimeError("agent loop is not active"))
        self._inactive.set()
        await asyncio.gather(
            *[d() for d in list(self._live_agents)],
            *list(self._startup_tasks),
            return_exceptions=True,
        )


async def race_abort(operation, signal, id_) -> object:
    """await 操作,或信号一 abort 就抛出它的原因。

    返回原值(同步或异步);信号先到则抛错 —— 竞速语义与
    Promise.race 一致:操作不取消,由调用方的 release 回调收尾。
    """

    def to_abort_error():
        reason = signal.reason
        if isinstance(reason, BaseException):
            return reason
        return RuntimeError(f'agent "{id_}" creation aborted')

    if signal.aborted:
        raise to_abort_error()
    if not inspect.isawaitable(operation):
        # 同步原值无需竞速(TS 的 Promise.resolve 微任务同效)
        return operation
    loop = asyncio.get_running_loop()
    aborted = loop.create_future()

    def listener(reason) -> None:
        if not aborted.done():
            aborted.set_exception(to_abort_error())

    signal.add_listener(listener)
    task = asyncio.ensure_future(operation)
    try:
        done, _ = await asyncio.wait(
            {task, aborted}, return_when=asyncio.FIRST_COMPLETED
        )
        if aborted in done:
            raise to_abort_error()
        return task.result()
    finally:
        signal.remove_listener(listener)


async def race_abort_call(operation, signal, id_, release_abandoned=None) -> object:
    """启动一个可中止操作,取消后释放迟到到达的值。

    信号先 abort 时,后台操作继续;一旦它落地,release_abandoned
    消费被抛弃的值(如把未公布的 SessionPreparation 释放回缓存)。
    """
    if signal.aborted:
        reason = signal.reason
        if isinstance(reason, BaseException):
            raise reason
        raise RuntimeError(f'agent "{id_}" creation aborted')
    pending = asyncio.ensure_future(operation())
    try:
        return await race_abort(pending, signal, id_)
    except BaseException as error:
        if signal.aborted and release_abandoned is not None:
            async def _release() -> None:
                try:
                    value = await pending
                except BaseException:
                    return
                release_abandoned(value)

            asyncio.ensure_future(_release())
        raise


def resolve_max_parallel_tool_calls(value) -> int:
    """在配置边界解析部署级调度上限:缺省回落默认值,非法即拒绝。"""
    v = DEFAULT_MAX_PARALLEL_TOOL_CALLS if value is None else value
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise RuntimeError("maxParallelToolCalls must be a positive integer")
    return v


def assert_agent_options(options: dict) -> None:
    """拒绝无法在请求线上精确表示的输出 token 上限。"""
    max_tokens = options.get("maxTokens")
    if max_tokens is not None and (
        not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0
    ):
        raise TypeError("agent maxTokens must be a positive safe integer")


def apply_launcher_identities(agents: list, identities) -> list:
    """把启动器拥有的身份套到配置 agent 上,替换两个身份键。

    配置行提供的身份与启动器身份永不共存:launcher 命名过的条目
    全部替换 —— 覆盖层改模型路由时不会意外丢身份。
    """
    if identities is None:
        return agents
    out = []
    for agent in agents:
        identity = identities.get(agent.get("id"))
        if identity is None:
            out.append(agent)
            continue
        rest = {k: v for k, v in agent.items()
                if k not in ("sessionId", "resumeSessionId")}
        key = "resumeSessionId" if identity["resume"] else "sessionId"
        out.append({**rest, key: identity["id"]})
    return out


def validate_configured_agents(agents: list) -> None:
    """在任何配置 agent 启动前拒绝自相矛盾的身份声明。

    两条规则:一个条目不能同时声明 sessionId 与 resumeSessionId;
    两个条目不能共享同一个精确会话身份(精确身份 = 全局唯一键)。
    """
    exact_identities = {}
    for agent in agents:
        id_ = agent["id"]
        session_id = agent.get("sessionId")
        resume_id = agent.get("resumeSessionId")
        has_resume_id = resume_id is not None and resume_id != ""
        if session_id is not None and has_resume_id:
            raise RuntimeError(
                f'agent "{id_}": sessionId and resumeSessionId are mutually exclusive'
            )
        exact_identity = resume_id if has_resume_id else session_id
        if exact_identity is None:
            continue
        first_id = exact_identities.get(exact_identity)
        if first_id is not None:
            raise RuntimeError(
                f'agents "{first_id}" and "{id_}" use duplicate exact session identity "{exact_identity}"'
            )
        exact_identities[exact_identity] = id_


#: settings 命名空间常量保留:批次 3 的 settings 面落地前不注册。
AGENT_LOOP_SETTINGS_NAMESPACE = "agent-loop"


def _agent_option(context: dict, key: str):
    """提示变量的 provider:读发起 agent 的循环选项,无 agent 则 None。"""
    agent = context.get("agent")
    if agent is None:
        return None
    options = getattr(agent, "options", None) or {}
    return options.get(key)


def _agent_cwd(context: dict):
    """提示变量 cwd:读发起 agent 的会话工作目录。"""
    agent = context.get("agent")
    if agent is None:
        return None
    session = getattr(agent, "session", None)
    if session is None:
        return None
    return session.header.get("cwd")


class AgentLoop(Service, AgentFactory):
    """具体 agent 工厂与驱动服务(ctx.agentLoop)。

    生命周期编排:create/resume 家族全部经 ``prepare`` 构造
    「agent + 作用域 + 单一记忆化逆序拆解」,发布前拆解已注册
    (中途卸载可完整回滚);配置声明的 agent 在构造时启动
    (restore/resume 双路径,身份精确,失败经事件向身份绑定
    消费者报告而非静默)。
    """

    inject = ["agents", "sessions", "llm", "tools", "systemPrompt"]

    def __init__(self, ctx, config: dict) -> None:
        super().__init__(ctx, "agentLoop")
        entry = {
            "maxParallelToolCalls": resolve_max_parallel_tool_calls(
                config.get("maxParallelToolCalls")
            ),
        }
        # settings 面(批次 3)落地前,配置上限一次解析、固定生效;
        # tool_calls.py 每群开始读这个值,部署级修改在重建后生效。
        self.config = {
            **config,
            "agents": apply_launcher_identities(
                config.get("agents") or [],
                ctx.get(CONFIGURED_AGENT_IDENTITIES_KEY),
            ),
            "maxParallelToolCalls": entry["maxParallelToolCalls"],
        }
        validate_configured_agents(self.config["agents"])
        self._ownership = FactoryOwnership(ctx.fiber)
        #: 普通持有者:阻止 cordis 把工厂的依赖上下文经调用方
        #: 阴影重溯(TS runtime 注释同款)。
        self._runtime = {"ctx": ctx}

        ctx.effect(lambda: self._ownership.dispose, "agentLoop.transactions()")
        ctx.effect(lambda: ctx.agents.set_factory(self), "agentLoop.setFactory()")
        # 提示变量:agent 循环属主提供 provider/model/cwd,装配时
        # 经 agent 上下文求值 —— 作用域变量影子全局值。
        ctx.systemPrompt.variable("provider", lambda context: _agent_option(context, "provider"))
        ctx.systemPrompt.variable("model", lambda context: _agent_option(context, "model"))
        ctx.systemPrompt.variable("cwd", lambda context: _agent_cwd(context))

        for agent_cfg in self.config["agents"]:
            id_ = agent_cfg["id"]
            session_id = agent_cfg.get("sessionId")
            cwd = agent_cfg.get("cwd")
            resume_session_id = agent_cfg.get("resumeSessionId")
            options = {k: v for k, v in agent_cfg.items()
                       if k not in ("id", "sessionId", "cwd", "resumeSessionId")}
            meta = {"cwd": cwd} if cwd is not None else {}
            if resume_session_id is None or resume_session_id == "":
                # 无精确身份:新鲜组合 id;有精确身份:先尝试恢复
                # 物化历史(有 sessionId 才需要 persistence —— 随机
                # 新 id 不可能已有历史)。
                configured_id = (
                    session_id if session_id is not None
                    else SessionId(f"{id_}-session-{uuid.uuid4()}")
                )
                persistence = None if session_id is None else ctx.get("sessionPersistence")
                if persistence is None:
                    self.create(configured_id, options, meta)
                else:
                    startup = self._safe_restore(
                        id_, configured_id,
                        self._restore_or_create_configured(
                            ctx, persistence, configured_id, options, meta
                        ),
                    )
                    self._ownership.track_startup(startup)
                continue
            # resume 路径延迟到注入 sessionPersistence 的 fiber:
            # 配置启动等待持久化服务就绪,失败经同一报告通道。
            def _resume_effect():
                def _start(child_ctx):
                    async def _run():
                        try:
                            await self._resume_with(ctx, child_ctx.sessionPersistence, {
                                "resumeSessionId": resume_session_id,
                                "agentOptions": options,
                            })
                        except BaseException as error:
                            self._report_configured_startup_failure(
                                id_, "resume", resume_session_id, error
                            )

                    asyncio.get_running_loop().create_task(_run())

                fiber = ctx.inject(["sessionPersistence"], _start)
                return fiber.dispose

            ctx.effect(_resume_effect, f"agentLoop.resume({id_})")

    # ---- 配置启动失败报告 ----

    def _report_configured_startup_failure(
        self, config_id: str, action: str, session_id, error
    ) -> None:
        """向身份绑定消费者报告一个受控的声明式启动失败。

        正常工厂拆除抑制失败报告:取消的启动尝试不值得广播。
        """
        if not self._ownership.is_active():
            return
        self.ctx.logger.warn(
            f'agent "{config_id}": config-driven {action} of "{session_id}" '
            f"failed: {error_chain(error)}"
        )
        args = ["agent-loop/config-start-failed", {"sessionId": session_id, "error": error}]
        for callback in self.ctx.events.dispatch("emit", args):
            try:
                returned = callback(*args)
            except BaseException as listener_error:
                self.ctx.logger.warn(
                    f'agent "{config_id}": config-start-failed listener threw: '
                    f"{error_chain(listener_error)}"
                )
                continue
            if inspect.isawaitable(returned):
                try:
                    future = asyncio.ensure_future(returned)
                except RuntimeError:
                    continue  # 无运行 loop:同步监听者路径,无需收尾
                future.add_done_callback(
                    lambda task, cid=config_id: task.exception() is not None
                    and self.ctx.logger.warn(
                        f'agent "{cid}": config-start-failed listener rejected: '
                        f"{error_chain(task.exception())}"
                    )
                )

    async def _safe_restore(self, config_id: str, configured_id, coro) -> None:
        """restore 启动包装:失败归报告通道,不冒泡到工厂拆解。"""
        try:
            await coro
        except BaseException as error:
            self._report_configured_startup_failure(
                config_id, "restore", configured_id, error
            )

    # ---- 配置身份恢复 ----

    async def _restore_or_create_configured(
        self, owner_ctx, persistence, session_id, agent_options, meta
    ) -> None:
        """重挂时恢复物化的精确配置身份;首次使用时创建。

        加载是本身份的串行化屏障:只有在确定 artifact 真正缺席
        (列举不到)时才落到首次创建 —— 损坏与后端失败保持响亮,
        不静默改写历史。
        """
        await self._wait_for_draining_configured_identity(owner_ctx, session_id)
        if not self._ownership.is_active():
            return
        try:
            await self._resume_with(owner_ctx, persistence, {
                "resumeSessionId": session_id,
                "agentOptions": agent_options,
            })
            return
        except BaseException as error:
            if not self._ownership.is_active():
                return
            exists = any(
                header.get("id") == session_id
                for header in await persistence.list()
            )
            if exists:
                raise
        self.create(session_id, agent_options, meta)

    async def _wait_for_draining_configured_identity(self, owner_ctx, session_id) -> None:
        """等待排干中的同身份生命周期完成注册表拆解。

        只等仍在注册表里的 id:健康活着的同身份占用者由下方
        create/resume 自己暴露冲突。
        """
        if (owner_ctx.agents.get(session_id) is None
                and owner_ctx.sessions.get(session_id) is None):
            return
        loop = asyncio.get_running_loop()
        released = loop.create_future()

        def check_released() -> None:
            if (owner_ctx.agents.get(session_id) is None
                    and owner_ctx.sessions.get(session_id) is None
                    and not released.done()):
                released.set_result(None)

        dispose_agent_listener = owner_ctx.on(
            "agent/disposed", lambda payload: check_released()
        )
        dispose_session_listener = owner_ctx.on(
            "session/disposed", lambda payload: check_released()
        )
        try:
            check_released()
            await self._ownership.wait_while_active(released)
        finally:
            dispose_agent_listener()
            dispose_session_listener()

    # ---- 准备 / 发布 ----

    def _prepare(self, owner_ctx, id_, options, session, caller_signal=None) -> dict:
        """构造驱动、作用域与一个记忆化逆序拆解。

        拆解在发布**之前**注册进工厂与属主纤维:中途卸载可完整
        回滚;``signal`` 把调用方取消与生命周期拆除融合成 setup
        await 的单一中止源 —— 三类属主(调用方取消、属主卸载、
        工厂拆除)各自携带自己的原因。

        @returns {'agent', 'signal', 'publish', 'dispose'}。
        """
        assert_agent_options(options)
        owner_ctx.fiber.assert_active()
        # 调用方都同步到达此点(cordis 派发已要求活工厂纤维);
        # 防御性兜底,防未来路径绕过。
        if not self._ownership.is_active():
            raise RuntimeError("agent loop is not active")
        if caller_signal is not None and caller_signal.aborted:
            reason = caller_signal.reason
            if isinstance(reason, BaseException):
                raise reason
            raise RuntimeError(f'agent "{id_}" creation aborted')
        loop_ctx = self._runtime["ctx"]

        # 反激活融合三个属主;在任何资源存在前注册(可变槽位),
        # 卸载在作用域铸造途中到达也能找到工作的清理器。
        abort = AbortController()

        def on_caller_abort(reason) -> None:
            reason = caller_signal.reason if caller_signal is not None else None
            abort.abort(reason if isinstance(reason, BaseException)
                        else RuntimeError(f'agent "{id_}" creation aborted'))

        def on_factory_teardown(reason) -> None:
            abort.abort(self._ownership.signal.reason)

        if caller_signal is not None:
            caller_signal.add_listener(on_caller_abort)
        self._ownership.signal.add_listener(on_factory_teardown)

        machine = [None]
        detach_session = [None]
        detach_agent = [None]
        disposing = [None]
        machine_ready = asyncio.Event()

        async def _dispose_impl(owner_triggered: bool = False) -> None:
            # 逆序拆解,备忘:并发属主 await 同一静默 —— 停止机器、
            # 离开注册表、展开作用域、释放簿记。
            abort.abort(RuntimeError(f'agent "{id_}" lifecycle disposed'))
            if caller_signal is not None:
                caller_signal.remove_listener(on_caller_abort)
            self._ownership.signal.remove_listener(on_factory_teardown)
            try:
                # 拆解本身就是 disposed 原因取消 + 静默等待;此后的
                # 新工作是发送方的 bug —— 注册表即将丢弃 agent。
                if machine[0] is None:
                    await machine_ready.wait()
                if machine[0] is not None:
                    machine[0].cancel({"kind": "disposed"})
                    await machine[0].when_idle()
                    await machine[0].scope.dispose()
            finally:
                try:
                    if detach_agent[0] is not None:
                        detach_agent[0]()
                    if detach_session[0] is not None:
                        detach_session[0]()
                finally:
                    untrack()
                    if not owner_triggered:
                        await unfollow_owner()

        def dispose(owner_triggered: bool = False):
            """备忘的拆解入口:首触发落地,后续调用返回同一 future。"""
            if disposing[0] is None:
                disposing[0] = asyncio.ensure_future(_dispose_impl(owner_triggered))
            return disposing[0]

        untrack = self._ownership.track(dispose)

        def _lifecycle_effect():
            def cleanup():
                # 属主拆除拥有同一静默边界;从自身内部注销这个
                # 已在跑的属主 effect 无意义,跳过。
                if disposing[0] is not None:
                    return
                abort.abort(RuntimeError(
                    f'agent "{id_}" setup aborted: owner disposed during setup'
                ))
                return dispose(True)

            return cleanup

        try:
            unfollow_owner = owner_ctx.effect(
                _lifecycle_effect, f"agentLoop.lifecycle({id_})"
            )
        except BaseException as error:
            untrack()
            if caller_signal is not None:
                caller_signal.remove_listener(on_caller_abort)
            self._ownership.signal.remove_listener(on_factory_teardown)
            raise error

        def assert_live() -> None:
            if not abort.signal.aborted:
                return
            # 每个融合中止源都带 Error 原因;String() 臂是防御兜底。
            reason = abort.signal.reason
            if isinstance(reason, BaseException):
                raise reason
            raise RuntimeError(str(reason))

        try:
            agent = machine[0] = ReactLoopAgent(loop_ctx, id_, options, session)
            machine_ready.set()
            assert_live()
        except BaseException as error:
            machine_ready.set()
            _ = dispose()
            raise error

        def publish(source) -> AgentHandle:
            assert_live()
            detach_session[0] = agent.ctx.sessions.enter(session)
            detach_agent[0] = loop_ctx.agents.enter(agent, owner_ctx.agent)
            agent.ctx.sessions.announce(session)
            assert_live()
            loop_ctx.agents.announce(agent)
            assert_live()
            # 同步 announce/session-start 监听者可能已开始拆解;
            # 机器已活(投递从 session-start 扩展点开始工作),只需
            # 复查活性。
            emit_agent_event(loop_ctx, agent, "agent/session-start", {"source": source})
            assert_live()
            return AgentHandle(agent, dispose)

        return {"agent": agent, "signal": abort.signal, "publish": publish, "dispose": dispose}

    # ---- 公共创建 / 恢复 ----

    def create(self, id_, options: dict | None = None, meta: dict | None = None):
        """在一个调用方提供的身份下创建 agent 与会话,归访问纤维所有。

        构造驱动的配置调用先铸新鲜组合 id 再进此边界。
        """
        options = options or {}
        meta = meta or {}
        preparation = SessionPreparation.create(
            self._runtime["ctx"].sessions.prepare(id_, {"meta": meta})
        )
        prepared = self._prepare(self.ctx, id_, options, preparation.session)
        try:
            try:
                return prepared["publish"]("startup").agent
            except BaseException as error:
                _ = prepared["dispose"]()
                raise error
        finally:
            # using 析构等价:发布或回滚后释放 provider 持有的未公布状态
            preparation.dispose()

    def create_agent(self, owner_ctx, options: dict):
        """在调用方提供的会话 id 上创建属主 agent(异步句柄)。"""
        prep_options = {}
        if options.get("seed") is not None:
            prep_options["seed"] = options["seed"]
        if options.get("meta") is not None:
            prep_options["meta"] = options["meta"]
        preparation = SessionPreparation.create(
            self._runtime["ctx"].sessions.prepare(
                options["sessionId"], prep_options
            )
        )
        published = self._setup_and_publish(
            owner_ctx,
            options["sessionId"],
            preparation,
            options.get("agentOptions") or {},
            options.get("setup"),
            options.get("signal"),
            "startup",
        )
        return self._ownership.track_wrapper(published)

    async def _setup_and_publish(
        self, owner_ctx, id_, preparation, agent_options,
        setup, signal, source,
    ) -> AgentHandle:
        """在一个已获取的 Session 上准备 Agent,运行 setup,发布它。"""
        session = preparation.session
        prepared = self._prepare(owner_ctx, id_, agent_options, session, signal)
        try:
            setup_commit = await race_abort(
                setup(prepared["agent"].ctx) if setup is not None else None,
                prepared["signal"],
                id_,
            )
            if setup_commit is not None:
                commit_fn = getattr(setup_commit, "commit", None)
                if commit_fn is not None:
                    commit_fn()
            return prepared["publish"](source)
        except BaseException as error:
            await prepared["dispose"]()
            raise error

    def resume(self, owner_ctx, options: dict):
        """从配置的持久化服务恢复属主 agent。"""
        persistence = self._runtime["ctx"].get("sessionPersistence")
        if persistence is None:
            raise RuntimeError(
                "cannot resume: session persistence is not configured "
                "(load a session-persistence backend)"
            )
        return self._resume_with(owner_ctx, persistence, options)

    def _resume_with(self, owner_ctx, persistence, options: dict):
        """经显式持久化句柄恢复(配置延迟路径同用)。"""
        id_ = options["resumeSessionId"]

        async def _run():
            # 加载可能比属主活得久:与调用方取消、属主 fiber 卸载、
            # 工厂拆除竞速 —— 永不结算的后端不能钉死身份。
            owner_abort = AbortController()

            def _load_effect():
                def cleanup() -> None:
                    owner_abort.abort(RuntimeError(
                        f'agent "{id_}" setup aborted: owner disposed during setup'
                    ))

                return cleanup

            unfollow_owner = owner_ctx.effect(_load_effect, f"agentLoop.resume-load({id_})")
            sources = [s for s in (
                options.get("signal"), owner_abort.signal, self._ownership.signal
            ) if s is not None]
            fused = any_signals(sources)
            preparation = None
            try:
                try:
                    preparation = await race_abort_call(
                        lambda: persistence.prepare(id_),
                        fused,
                        id_,
                        lambda abandoned: abandoned.dispose(),
                    )
                finally:
                    await unfollow_owner()
                owner_ctx.fiber.assert_active()
                if not self._ownership.is_active():
                    raise RuntimeError("agent loop is not active")
                return await self._setup_and_publish(
                    owner_ctx,
                    id_,
                    preparation,
                    options.get("agentOptions") or {},
                    options.get("setup"),
                    options.get("signal"),
                    "resume",
                )
            finally:
                if preparation is not None:
                    preparation.dispose()

        return self._ownership.track_wrapper(_run())
