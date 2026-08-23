"""Entry — translation of vendor/loader/src/config/entry.ts.

一行配置 → 插件实例(fiber)。关键语义:
- ``Entry`` **不是 Fiber 子类**(deepseek fork):持有 ``fiber`` 字段;
  ``fiber.entry`` 在 ``_start`` 创建后立即挂(TS 经 internal/plugin hook
  注入,但 root 绑定 registry 下不可达 —— 见 ``_start`` 注释)
- ``Entry.key`` 牌子 → ``ENTRY`` 字符串键,挂在 entry 专属子 ctx 上
- ``update`` 四情况:A 无 fiber → init(+失败还原);B 禁用 → dispose;
  C 非结构变更 → ``_patchContext``(+失败回滚重跑);D 结构变更
  (name/inject/group)→ re-import + dispose + ``_start``(+失败 re-start 旧插件)
- disabled 经祖先链传播;group 恒启用
- ``_patchContext`` 的 ctx re-point:``_fallback`` 槽改指父 ctx
  (TS ``Object.setPrototypeOf(entry.ctx, entry.parent.ctx)``)

``get_outer_stack`` 调用栈整形省略(TS composeError;Python 异常自带
traceback),参数保留以对齐 loader 调用点签名。
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from ..utils import AggregateError, FALLBACK, ENTRY
from .utils import evaluate, is_js_expr

if TYPE_CHECKING:
    from .group import EntryGroup
    from .tree import EntryTree

__all__ = ["Entry", "LoaderEntryError"]


class LoaderEntryError(RuntimeError):
    """Failed to apply a loader entry(TS updateError 的消息形状)。"""

    def __init__(self, stage: str, options: dict, cause: Any) -> None:
        self.stage = stage
        self.options = options
        self.cause = cause
        detail = str(cause) if isinstance(cause, Exception) else str(cause)
        super().__init__(
            f"failed to {stage} loader entry {options['id']} ({options['name']}): {detail}"
        )


def deep_equal(a: Any, b: Any) -> bool:
    """cosmokit deepEqual 的 Python 等价(loader 配置 diff 用)。"""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return set(a) == set(b) and all(deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(deep_equal(x, y) for x, y in zip(a, b))
    return a == b


def sort_keys(obj: dict, prepend: tuple = ("id", "name"), append: tuple = ("config",)) -> None:
    """canonical key order:prepend + 字母序 rest + append(TS sortKeys,
    就地修改,保持对象 identity —— group.data 靠它定位)。"""
    part1 = [(k, obj.pop(k)) for k in prepend if k in obj]
    part2 = [(k, obj.pop(k)) for k in append if k in obj]
    rest = sorted(obj.items(), key=lambda item: item[0])
    obj.clear()
    obj.update(part1 + rest + part2)


def replace_keys(target: dict, source: dict) -> dict:
    """删除全部 own 键后装入 source(TS replaceKeys:对象 identity 保持)。"""
    target.clear()
    target.update(source)
    return target


class Entry:
    key = ENTRY

    def __init__(self, loader: Any) -> None:
        self.loader = loader
        self.ctx = loader.ctx.extend({self.key: self})
        self.context = self.ctx
        self.fiber: Any = None
        self.parent: "EntryGroup | None" = None
        # safety: call `entry.update()` immediately after creating an entry
        self.options: dict[str, Any] = {}
        self.subgroup: "EntryGroup | None" = None
        self.subtree: "EntryTree | None" = None

        self._init_task: Any = None
        self._disposing = 0

        self.context.emit("loader/entry-init", self)

    # --- 基本信息 ---

    @property
    def id(self) -> str:
        from .tree import EntryTree  # 延迟 import 破循环(tree ← entry)

        id_ = self.options["id"]
        entry = getattr(self.parent.tree.ctx.fiber, "entry", None)
        if entry:
            id_ = entry.id + EntryTree.sep + id_
        return id_

    @property
    def disabled(self) -> bool:
        """True when this entry or any owning parent entry is disabled."""
        return self._disabled(self.options)

    def _disabled(self, options: dict) -> bool:
        # group is always enabled
        if options.get("group"):
            return False
        if self._disabled_of(options):
            return True
        entry = getattr(self.parent.ctx.fiber, "entry", None)
        while entry:
            if self._disabled_of(entry.options):
                return True
            entry = getattr(entry.parent.ctx.fiber, "entry", None)
        return False

    def _disabled_of(self, options: dict) -> bool:
        """Effective disabled state: a `!!js` expression evaluates against the
        loader context. The raw node stays in the options, so write-back keeps
        the form."""
        value = options.get("disabled")
        if is_js_expr(value):
            return bool(self.evaluate(value["__jsExpr"]))
        return bool(value)

    def evaluate(self, expr: str) -> Any:
        return evaluate(self.ctx, expr)

    # --- 生命周期 ---

    async def _patch_context(self, diff: list[str]) -> None:
        async def inner() -> None:
            object.__setattr__(self.ctx, FALLBACK, self.parent.ctx)
            if self.fiber is not None and self.fiber.uid is not None and (
                "config" in diff or self.options.get("group")
            ):
                await self.fiber.update(self.options.get("config"), True)

        # events.waterfall 是同步链,监听者可能是 async —— await 结果展开
        result = self.context.waterfall("loader/patch-context", self, inner)
        if inspect.isawaitable(result):
            await result

    async def refresh(self) -> None:
        if self.fiber is not None:
            return
        if self.disabled:
            return
        await self.init()

    async def _dispose(self, fiber: Any = None) -> None:
        if fiber is None:
            fiber = self.fiber
        if fiber is None:
            return
        if self.fiber is fiber:
            self.fiber = None
        self._disposing += 1
        try:
            await fiber.dispose()
        finally:
            self._disposing -= 1

    # --- 更新 ---

    async def update(self, options: dict, create: bool = False, force: bool = False) -> None:
        previous_options = self.options
        legacy = dict(previous_options)
        candidate = options if create else dict(previous_options)
        if not create:
            for key, value in options.items():
                if value is None:
                    candidate.pop(key, None)
                else:
                    candidate[key] = value
        sort_keys(candidate)

        diff = [
            key
            for key in set(candidate) | set(legacy)
            if not deep_equal(candidate.get(key), legacy.get(key))
        ]
        if not diff and not force:
            return

        def commit() -> None:
            if create:
                return
            replace_keys(previous_options, candidate)

        previous = self.fiber
        if previous is None or previous.uid is None:
            self.fiber = None
            self.options = candidate
            try:
                if not self._disabled(candidate):
                    await self.init()
            except BaseException as error:
                self.options = previous_options
                raise error from None
            commit()
            return

        if self._disabled(candidate):
            self.options = candidate
            try:
                await self._dispose(previous)
            except BaseException as error:
                self.options = previous_options
                raise update_error("dispose", candidate, error) from None
            commit()
            self.context.emit("loader/partial-dispose", self, legacy, True)
            return

        replace = any(key in ("name", "inject", "group") for key in diff)
        if not replace:
            self.options = candidate
            try:
                await self._patch_context(diff)
            except BaseException as error:
                self.options = previous_options
                try:
                    await self._patch_context(diff)
                except BaseException as rollback_error:
                    raise update_error(
                        "rollback", legacy, AggregateError([error, rollback_error])
                    ) from None
                self.context.emit("loader/partial-dispose", self, candidate, True)
                raise update_error("apply", candidate, error) from None
            commit()
            self.context.emit("loader/partial-dispose", self, legacy, True)
            return

        try:
            plugin = (
                self.loader.unwrap_exports(
                    await self.parent.tree.import_module(candidate["name"], self.get_outer_stack)
                )
                if "name" in diff
                else previous.runtime.callback
            )
        except BaseException as error:
            raise update_error("import", candidate, error) from None

        previous_plugin = previous.runtime.callback
        self.options = candidate
        try:
            await self._dispose(previous)
        except BaseException as error:
            self.options = previous_options
            raise update_error("dispose", candidate, error) from None

        try:
            await self._start(plugin)
        except BaseException as error:
            self.options = previous_options
            try:
                await self._start(previous_plugin)
            except BaseException as rollback_error:
                raise update_error(
                    "rollback", legacy, AggregateError([error, rollback_error])
                ) from None
            self.context.emit("loader/partial-dispose", self, candidate, True)
            raise update_error("apply", candidate, error) from None
        commit()
        self.context.emit("loader/partial-dispose", self, legacy, True)

    def get_outer_stack(self) -> list[str]:
        entry: "Entry | None" = self
        result: list[str] = []
        while entry:
            result.append(f"    at {entry.parent.tree.ctx.baseUrl}#{entry.options['id']}")
            entry = getattr(entry.parent.ctx.fiber, "entry", None)
        return result

    # --- 启动 ---

    async def init(self) -> None:
        try:
            task = self._init_task
            if task is None:
                task = self._init()
                self._init_task = task
            await task
        finally:
            self._init_task = None
            if not self.loader.get_tasks():
                self.ctx.reflect.notify(["loader"])
        await self._await()

    async def _await(self) -> None:
        try:
            if self.fiber is not None:
                await self.fiber.wait()
        except BaseException as error:
            raise update_error("apply", self.options, error) from None

    async def _init(self) -> None:
        try:
            plugin = self.loader.unwrap_exports(
                await self.parent.tree.import_module(self.options["name"], self.get_outer_stack)
            )
        except BaseException as error:
            raise update_error("import", self.options, error) from None
        try:
            await self._start(plugin)
        except BaseException as error:
            raise update_error("apply", self.options, error) from None

    async def _start(self, plugin: Any) -> None:
        fiber: Any = None
        try:
            await self._patch_context([])
            self.loader.show_log(self, "apply")
            fiber = self.fiber = self.ctx.registry.plugin(
                plugin,
                self.options.get("config"),
                self.get_outer_stack,
                # TS `this.ctx.plugin()` mixin 显式传 this 为 parent;Python
                # mixin 绑定丢失 ctx,须显式传 entry.ctx(fiber ctx 由此继承
                # entry 的 isolate/intercept map)
                parent=self.ctx,
            )
            # TS 的构造期注入在 root 绑定 registry 下不可达(见
            # loader/__init__.py `_on_internal_plugin` 注释 —— fork 死代码);
            # Python 在创建后立即挂 entry,语义等价(case 1 uid 早退使时序无关)
            fiber.entry = self
            await fiber.wait()
        except BaseException as error:
            if fiber is not None:
                await self._dispose(fiber)
            raise error from None


def update_error(stage: str, options: dict, cause: Any) -> LoaderEntryError:
    return LoaderEntryError(stage, options, cause)
