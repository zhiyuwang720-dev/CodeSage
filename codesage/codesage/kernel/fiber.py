"""Fiber — translation of vendor/cordis/src/fiber.ts.

一个 Fiber = 一个插件运行时实例:依赖跟踪、配置校验、生命周期效果、
清理。状态机 PENDING → LOADING → ACTIVE / FAILED,卸载经 UNLOADING →
DISPOSED;依赖变化(epoch)驱动 _reload/_unload。

Python 映射差异(见 docs/modules/21 映射表):
- Config 校验:TS StandardSchema → Python 用构造器(dataclass)校验,
  失败抛 ValidationError
- effect:disposer 为 async 函数时 dispose 先 await 收集任务再逆序执行
  (TS 靠 effectInertia 竞态协调,Python 顺序化更安全)
- getEffects() 的 children 树不收集(只记 label)
"""

from __future__ import annotations

import asyncio
import inspect
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from .utils import FALLBACK, DisposableList, INIT_HOOKS, INIT, is_constructor, is_object

if TYPE_CHECKING:
    from .context import Context

INACTIVE = "__INACTIVE__"


class FiberState(Enum):
    """插件生命周期状态(TS FiberState)。"""

    PENDING = "pending"       # 等待所需服务
    LOADING = "loading"       # 插件回调运行中
    ACTIVE = "active"         # 已加载并对外提供
    FAILED = "failed"         # 回调或配置抛错
    DISPOSED = "disposed"     # 已移除,不可重启
    UNLOADING = "unloading"   # disposer 运行中


class CordisError(Exception):
    """框架错误,带稳定机器可读错误码(TS CordisError)。"""

    INACTIVE_EFFECT = "cannot create effect on inactive context"

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class ValidationError(TypeError):
    """插件配置校验失败(TS ValidationError,标准 schema issues)。"""

    def __init__(self, issues: list[str]) -> None:
        super().__init__("invalid config:\n" + "\n".join(f"  - {issue}" for issue in issues))


def resolve_config(runtime: Any, config: Any) -> Any:
    """校验并规范化插件配置;无 Config 原样返回。Config 为构造器(如
    dataclass):dict 配置按 kwargs 构造(多余的键抛错),其余按位置构造;
    失败抛 ValidationError。
    (比 TS StandardSchema 弱:不含类型检查,映射表已注明)"""
    Config = runtime.Config
    if Config is None:
        return config
    try:
        if isinstance(config, dict):
            return Config(**config)
        return Config(config)
    except TypeError as e:
        raise ValidationError([str(e)]) from None


def emit_plugin_disposed(context: "Context", fiber: "Fiber") -> None:
    """通知插件卸载,不让单个观察者破坏所有权清理(TS 同义函数)。"""
    args: list[Any] = ["internal/plugin", fiber]
    try:
        callbacks = context.events.dispatch("emit", args)
    except Exception as error:
        context.logger.error(error)
        return
    for callback in callbacks:
        try:
            returned = callback(*args)
            if inspect.isawaitable(returned):
                asyncio.ensure_future(returned).add_done_callback(
                    lambda t: t.exception() and context.logger.error(t.exception())
                )
        except Exception as error:
            context.logger.error(error)


class _Runner:
    """Effect 执行器:记录 epoch + 执行插件回调 + 收集 disposer。"""

    def __init__(self, fiber: "Fiber") -> None:
        self.fiber = fiber
        self.epoch: Any = INACTIVE

    def execute(self) -> Any:
        runtime = self.fiber.runtime
        if isinstance(runtime.callback, type):
            instance = runtime.callback(self.fiber.ctx, self.fiber.config)
            for hook in getattr(instance, INIT_HOOKS, None) or []:
                hook()
            init = getattr(instance, INIT, None)
            return init() if callable(init) else None
        return runtime.callback(self.fiber.ctx, self.fiber.config)


class Fiber:
    def __init__(
        self,
        parent: "Context",
        config: Any,
        inject: dict[str, Any],
        runtime: Any | None,
        get_outer_stack: Callable[[], list] | None = None,
    ) -> None:
        self.parent = parent
        self._config = config
        self.inject: dict[str, Any] = inject
        self.runtime = runtime
        self._get_outer_stack = get_outer_stack or (lambda: [])

        self.uid: int | None = None
        self.ctx: "Context"
        self.config: Any = None
        self.state = FiberState.PENDING
        self.dispose: Callable[[], Any]
        self.store: dict[str, Any] | None = None
        self.inertia: Any = None

        self._hooks: dict[str, DisposableList] = {}
        self._disposables = DisposableList()
        self.context: "Context"
        self._error: Any = None
        self._runner: _Runner | None = None
        self._store: dict[str, Any] = {}

        if runtime:
            from .context import Context  # 延迟 import 防环

            self.uid = parent.registry.counter
            self.ctx = self.context = parent.extend({"fiber": self})
            # 原型链:TS Object.create(parent) 的运行时等价 —— 父 ctx 后加的
            # 真实属性(loader 的 entry/delim 等)沿 _fallback 可见
            object.__setattr__(self.ctx, FALLBACK, parent)

            # inject 拦截配置:写时复制 intercept 链(TS Object.create 原型链)
            entries = {k: v for k, v in self.inject.items() if v is not None}
            if entries:
                from .utils import INTERCEPT

                new_intercept = dict(getattr(self.ctx, INTERCEPT))
                new_intercept.update(entries)
                setattr(self.ctx, INTERCEPT, new_intercept)

            self._runner = _Runner(self)
            self.dispose = parent.fiber.effect(self._dispose_effect, "ctx.plugin()")

            # 发布后才解析依赖(loader 可能在此扩展 inject)
            self.context.emit("internal/plugin", self)
            if self.uid is not None and parent.fiber.state is not FiberState.UNLOADING:
                for name in self.inject:
                    self._check_impl(name)
                self._refresh()
        else:
            # root fiber
            self.uid = 0
            self.ctx = self.context = parent
            self.state = FiberState.ACTIVE
            self.store = {}
            self._runner = _Runner(self)
            self.dispose = self.restart

    # --- 基本信息 ---

    @property
    def name(self) -> str:
        """最近的有名 runtime 祖先的显示名,否则 'root'。"""
        fiber: Fiber = self
        while True:
            if fiber.runtime and fiber.runtime.name:
                return fiber.runtime.name
            fiber = fiber.parent.fiber
            if fiber is fiber.parent.fiber:
                return "root"

    def assert_active(self) -> None:
        """已销毁的 fiber 上创建 effect 会抛 CordisError('INACTIVE_EFFECT')。"""
        if self.uid is None:
            raise CordisError(CordisError.INACTIVE_EFFECT)

    # --- effect 系统 ---

    @staticmethod
    def _safe_collect(item: Any, collect: Callable) -> None:
        """TS _execute 的 safeCollect:函数收集,None 跳过,其余抛错。"""
        if callable(item):
            collect(item)
        elif item is None:
            return
        else:
            raise TypeError("Invalid effect")

    async def _collect_result(self, result: Any, collect: Callable) -> None:
        """TS _execute 的 Effect 形状分发:单 disposer / awaitable /
        iterable / async-iterable。"""
        if callable(result):
            collect(result)
        elif result is None:
            return
        elif inspect.isawaitable(result):
            resolved = await result
            self._safe_collect(resolved, collect)
        elif hasattr(result, "__aiter__"):
            async for item in result:
                self._safe_collect(item, collect)
        elif hasattr(result, "__iter__"):
            for item in result:
                self._safe_collect(item, collect)
        else:
            raise TypeError("Invalid effect")

    def effect(self, execute: Callable[[], Any], label: str = "anonymous") -> Callable:
        """注册可清理副作用:execute 立即执行,产出的 disposer(s) 在
        disposer 调用或 fiber 卸载时逆序执行;disposer 幂等(once)。"""
        self.assert_active()
        if self.state is FiberState.UNLOADING:
            raise CordisError(CordisError.INACTIVE_EFFECT)

        disposables: list[Callable] = []
        pending: list[Any] = []
        disposing = False
        in_flight: Any = None
        meta = {"label": label, "children": []}

        def collect(dispose: Callable) -> None:
            disposables.append(dispose)
            d_meta = getattr(dispose, "__cordis_effect__", None)
            if d_meta is not None:
                meta["children"].append(d_meta)

        def run() -> None:
            result = execute()
            # awaitable / async-iterable 形状需异步收集;无运行 loop 则无法
            # 调度(同步上下文里这些形状本就不可用)
            if inspect.isawaitable(result) or hasattr(result, "__aiter__"):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    raise TypeError(
                        "Invalid effect: async effect requires a running event loop"
                    ) from None
                pending.append(asyncio.create_task(self._collect_result(result, collect)))
            elif callable(result):
                collect(result)
            elif result is None:
                pass
            elif hasattr(result, "__iter__"):
                for item in result:
                    Fiber._safe_collect(item, collect)
            else:
                raise TypeError("Invalid effect")

        try:
            run()
        except BaseException:
            # 同步失败:已收集的 disposer 立即回滚(异步的挂后台,错误记日志)
            for d in reversed(disposables):
                try:
                    result = d()
                    if inspect.isawaitable(result):
                        try:
                            asyncio.ensure_future(result).add_done_callback(
                                lambda t: t.exception()
                                and self.ctx.logger.error(t.exception())
                            )
                        except RuntimeError:
                            pass  # 无运行 loop,忽略(同步失败场景)
                except Exception as error:
                    self.ctx.logger.error(error)
            raise

        async def dispose() -> None:
            nonlocal disposing, in_flight
            if disposing:
                if in_flight is not None:
                    await in_flight
                return
            disposing = True
            if pending:
                await asyncio.gather(*pending)
            task = asyncio.create_task(self._run_disposers(list(reversed(disposables))))
            in_flight = task
            await task

        setattr(dispose, "__cordis_effect__", meta)
        self._disposables.push(dispose)
        return dispose

    async def _run_disposers(self, disposers: list[Callable]) -> None:
        for d in disposers:
            result = d()
            if inspect.isawaitable(result):
                await result

    def getEffects(self) -> list[dict]:
        """当前注册的效果元数据(诊断用,TS getEffects)。"""
        return [
            getattr(d, "__cordis_effect__", None)
            for d in self._disposables
            if getattr(d, "__cordis_effect__", None) is not None
        ]

    # --- 状态机 ---

    def _get_state(self) -> FiberState:
        if self.uid is None:
            return FiberState.DISPOSED
        if self._error:
            return FiberState.FAILED
        if self._runner and self._runner.epoch is not INACTIVE:
            return FiberState.ACTIVE
        return FiberState.PENDING

    def _update_state(self, callback: Callable[[], FiberState | None]) -> None:
        old_state = self.state
        self.state = callback() or self._get_state()
        if old_state is self.state:
            return
        # TODO internal/fiber-info
        self.context.emit("internal/status", self, old_state)

        # 仅 ACTIVE 与非 ACTIVE 间变化才通知依赖
        if old_state is not FiberState.ACTIVE and self.state is not FiberState.ACTIVE:
            return
        for impl in list(self.ctx.reflect.store.values()):
            if impl.fiber is not self:
                continue
            self.ctx.reflect.notify([impl.name])

    def _check_impl(self, name: str) -> None:
        impl = self.ctx.reflect._get_impl(name, True)
        if impl is None:
            self._store.pop(name, None)
            return
        try:
            if impl.check and not impl.check():
                self._store.pop(name, None)
                return
        except Exception as error:
            impl.fiber.ctx.logger.error(error)
            self._store.pop(name, None)
            return
        self._store[name] = impl

    def _refresh(self) -> None:
        """epoch = 依赖实现 uid 拼接;任一缺失 → INACTIVE。"""
        epoch: Any = ""
        for name in self.inject:
            impl = self._store.get(name)
            if impl is None:
                epoch = INACTIVE
                break
            epoch += ":" + str(impl.fiber.uid)
        self._set_epoch(epoch)

    def _set_epoch(self, epoch: Any) -> None:
        old_epoch = self._runner.epoch
        if epoch == old_epoch:
            return
        self._runner.epoch = epoch
        if self.inertia:
            return
        if epoch is not INACTIVE and old_epoch is INACTIVE:
            self._update_state(self._mark_loading)
        else:
            self._update_state(self._mark_unloading)

    def _mark_loading(self) -> FiberState:
        self.inertia = self._spawn(self._reload())
        return FiberState.LOADING

    def _mark_unloading(self) -> FiberState:
        self.inertia = self._spawn(self._unload())
        return FiberState.UNLOADING

    def _spawn(self, coro: Any) -> Any:
        """TS async 方法调用即产生 promise(微任务自动跑);Python 协程
        需调度才执行 → 有运行 loop 建 task,否则原样挂起等 await。
        (3.12 的 ensure_future 无 loop 时会建隐式 loop 的孤儿 task,
        须先查 get_running_loop 再建)"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return coro
        return asyncio.ensure_future(coro)

    def _resolve_config(self, config: Any) -> Any:
        config = self.context.waterfall(self, "internal/config", config, lambda: config)
        return resolve_config(self.runtime, config) if self.runtime else config

    async def _reload(self) -> None:
        self.store = dict(self._store)
        old_epoch = self._runner.epoch
        try:
            await asyncio.sleep(0)  # Promise.resolve() 等价:先让状态发布
            if self._runner.epoch is old_epoch:
                self.config = self._resolve_config(self._config)
                result = self._runner.execute()
                await self._collect_result(result, self._disposables.push)
                self._error = None
        except Exception as reason:
            self.ctx.logger.error(reason)
            self._error = reason
            self._runner.epoch = INACTIVE
        # TS 同款:以 _updateState 收尾(成功 → _getState 推 ACTIVE)
        self._update_state(lambda: self._finish_reload(old_epoch))

    def _finish_reload(self, old_epoch: Any) -> FiberState | None:
        if self._runner.epoch is old_epoch:
            self.inertia = None
        else:
            self.inertia = self._spawn(self._unload())
            return FiberState.UNLOADING
        return None

    async def _unload(self) -> None:
        disposers = self._disposables.clear()

        async def safe_run(d: Callable) -> None:
            try:
                result = d()
                if inspect.isawaitable(result):
                    await result
            except Exception as reason:
                self.ctx.logger.error(reason)

        await asyncio.gather(*(safe_run(d) for d in disposers))
        self.store = None
        self._update_state(self._finish_unload)

    def _finish_unload(self) -> FiberState | None:
        if self._runner.epoch is INACTIVE:
            self.inertia = None
        else:
            self.inertia = self._spawn(self._reload())
            return FiberState.LOADING
        return None

    # --- 公开 API ---

    async def wait(self) -> "Fiber":
        """等当前生命周期工作结束;启动错误会重新抛出。
        (TS 方法名为 ``await``,Python 保留字 → wait)"""
        while self.inertia:
            await self.inertia
        if self._error:
            raise self._error
        return self

    def __await__(self):
        """await fiber → 等装载完成(TS 的 PromiseLike 包装)。"""
        return self.wait().__await__()

    async def restart(self) -> None:
        """卸载并立即用当前配置重载。"""
        self.assert_active()
        self._set_epoch(INACTIVE)
        self._refresh()
        await self.wait()

    def update(self, config: Any, no_save: bool = False) -> Any:
        """校验并应用新配置后重启(可被 internal/update waterfall 否决)。"""
        self.assert_active()
        self._config = config
        if self.state is not FiberState.ACTIVE:
            self._error = None
            self._set_epoch(INACTIVE)
            self._refresh()
            return None
        config = self._resolve_config(config)
        return self.context.waterfall(
            self,
            "internal/update",
            config,
            no_save,
            lambda: self._apply_update(config),
        )

    async def _apply_update(self, config: Any) -> Any:
        self.config = config
        self._error = None
        # TS: `return this.restart()` 的 Promise 由 internal/update 链末端
        # await;Python 同步 waterfall 只 await 一层的返回值 —— 不 await
        # restart 协程会被丢弃(组禁用传播/配置热更全部断链),须显式展开
        return await self.restart()

    # --- 内部:dispose effect(注册到父 fiber)---

    def _dispose_effect(self) -> Callable:
        remove = self.runtime.fibers.push(self)

        async def disposer() -> None:
            self.uid = None
            emit_plugin_disposed(self.context, self)
            if self.ctx.registry.has(self.runtime.callback):
                remove()
                if not self.runtime.fibers:
                    self.ctx.registry.delete(self.runtime.callback)
            self._set_epoch(INACTIVE)
            # PENDING fiber 可能已被 internal/plugin 观察者注册 effect;
            # 其 epoch 仍为 INACTIVE,_set_epoch 无转移可驱动,显式卸载
            if not self.inertia:
                self._update_state(self._mark_unloading)
            while self.inertia:
                await self.inertia

        return disposer
