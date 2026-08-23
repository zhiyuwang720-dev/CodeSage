"""Loader — translation of vendor/loader/src/index.ts.

Loader 服务 = EntryTree 子类:持有 loader entry 树,import 配置的插件。
持久化由子类实现 ``write()``(Loader 根树内存 no-op;Include 落盘)。

安装的 hooks:
- ``internal/config``(global):树载体(Group/Include)config 保持字面;
  其余做 ``!!js`` 插值(表达式按 Python 求值)
- ``internal/update``(prepend):插件运行中改配置 → 回写
  ``entry.options.config`` + ``tree.write()`` 持久化
- ``internal/update``:reload 日志
- ``internal/plugin``:10 步自卸载检测 —— 插件自己 ``dispose``(没有
  loader 的移除行为时)→ 持久化 ``disabled: true``
- ``ctx.plugin(isolate)``:intercept/isolate 选项生效
- ``Service.check``:``await`` 拦截时 getTasks 非空 → pending

与 TS 的差异:
- internal.ts(Node 内部模块加载器)省略,``Loader.internal = None``;
  ``cordis:`` 前缀走 ``builtins`` 注册表
- Service.tracker(ctx 关联追踪)省略;composeError 栈整形省略
- ``exit()`` 进程重启钩子:保留 no-op(宿主可覆写)
"""

from __future__ import annotations

import inspect
import json
import os
import time
from typing import TYPE_CHECKING, Any

from ..fiber import FiberState
from ..registry import resolve_inject
from ..service import Service
from .entry import Entry, LoaderEntryError
from .group import EntryGroup, Group
from .isolate import GlobalRealm, LocalRealm, Realm, _ctx_lookup, isolate
from .tree import EntryTree
from .utils import evaluate, interpolate, is_js_expr

if TYPE_CHECKING:
    from ..context import Context

__all__ = [
    "Entry",
    "EntryGroup",
    "EntryTree",
    "LoaderEntryError",
    "Group",
    "Loader",
    "Realm",
    "LocalRealm",
    "GlobalRealm",
    "evaluate",
    "interpolate",
    "is_js_expr",
]


class Loader(EntryTree):
    name = "loader"

    def __init__(self, ctx: "Context", config: dict | None = None) -> None:
        super().__init__(ctx)
        self.config = config or {}
        self.env_data = self._env_data()
        #: Node 内部模块加载器省略(纯 Node) —— 相对 specifier 由 tree 直接解析
        self.internal: Any = None
        #: ``cordis:name`` 前缀的插件注册表
        self.builtins: dict[str, Any] = {}

        if self.config.get("baseUrl"):
            object.__setattr__(self.ctx, "baseUrl", self.config["baseUrl"])

        ctx.reflect.provide("loader", self, self.check)

        ctx.on("internal/config", self._on_internal_config, {"global": True})
        ctx.on("internal/update", self._on_internal_update, {"global": True, "prepend": True})
        ctx.on("internal/update", self._on_internal_update_log, {"global": True})
        ctx.on("internal/plugin", self._on_internal_plugin)

        # TS 构造器里 fire-and-forget;Python 的 asyncio.run 收尾会取消未
        # await 的任务 —— 在 __cordis_init__ 里 await(见下)。parent 显式
        # 传 ctx(mixin 绑定丢失调用方 ctx,见 registry.plugin 文档)
        self._isolate_fiber = ctx.registry.plugin(isolate, parent=ctx)

    @staticmethod
    def _env_data() -> dict:
        shared = os.environ.get("CORDIS_SHARED")
        if shared:
            return json.loads(shared)
        return {"startTime": time.time()}

    def write(self) -> None:
        # Loader's root tree is in-memory; writes are no-ops.
        pass

    def check(self) -> bool:
        config: dict = Service.__cordis_resolve_config__(self)
        if config.get("await") and self.get_tasks():
            return False
        return True

    async def __cordis_init__(self) -> None:
        """等待 isolate 插件 fiber(TS 微任务语义的 Python 显式化)。"""
        await self._isolate_fiber.wait()

    # --- loader hooks ---

    def _current_fiber(self) -> Any:
        """internal/* 事件的派发 thisArg(TS ``function (this: Fiber)``)。"""
        return self.ctx.events._dispatch_this

    def _entry_of(self, fiber: Any) -> Entry | None:
        return getattr(fiber, "entry", None) if fiber is not None else None

    def _is_tree_carrier(self, fiber: Any) -> bool:
        """父 fiber 的 entry 与当前 entry 相同 → 树载体(Group/Include)。"""
        parent = getattr(fiber.parent, "fiber", None)
        entry = self._entry_of(fiber)
        return not entry or (
            parent is not None and getattr(parent, "entry", None) is entry
        )

    def _on_internal_config(self, _config: Any, next_fn: Any) -> Any:
        fiber = self._current_fiber()
        config = next_fn()
        if self._is_tree_carrier(fiber):
            return config
        # Tree carriers (Group, Include) keep their configs literal: their
        # entry and patch lists hold other rows' configs, whose `!!js`
        # expressions belong to those rows' own fibers.
        plugin = fiber.runtime.callback if fiber.runtime else None
        if plugin is not None and getattr(plugin, EntryGroup.key, None):
            return config
        return interpolate(fiber.ctx, config)

    async def _on_internal_update(self, config: Any, no_save: bool, next_fn: Any) -> Any:
        fiber = self._current_fiber()
        if self._is_tree_carrier(fiber) or no_save:
            # TS `return next()` 的 Promise 被 async 函数自动展平;Python
            # 协程作为返回值会被丢弃,须显式 await 链结果
            result = next_fn()
            if result is not None and inspect.isawaitable(result):
                return await result
            return result
        result = next_fn()
        if result is not None and inspect.isawaitable(result):
            await result
        unparse = getattr(getattr(fiber.runtime, "Config", None), "simplify", None)
        entry = self._entry_of(fiber)
        entry.options["config"] = unparse(config) if unparse else config
        entry.parent.tree.write()
        return None

    def _on_internal_update_log(self, config: Any, no_save: Any, next_fn: Any) -> Any:
        fiber = self._current_fiber()
        if self._is_tree_carrier(fiber):
            return next_fn()
        self.show_log(self._entry_of(fiber), "reload")
        return next_fn()

    def _on_internal_plugin(self, fiber: Any) -> None:
        # 1. set `fiber.entry`
        # TS: `fiber.parent[Entry.key]` —— 但 registry 根绑定使 fiber.parent
        # 恒为 root,root 无 ENTRY 键 → 死代码(deepseek fork 同,其测试只
        # 依赖 entry 树 API)。Python 在 entry._start 创建后直接挂 entry,
        # 此分支保留以 1:1 形状(不触发:parent_entry 恒 None)。
        # ENTRY 是 "__" 前缀键 —— 走 __dict__ 链直查(getattr 会被
        # Context.__getattr__ 的特殊名拦截吃掉)
        parent_entry = _ctx_lookup(fiber.parent, Entry.key)
        if parent_entry is not None and not getattr(fiber, "entry", None):
            fiber.entry = parent_entry
            # FIXME merge config
            resolve_inject(fiber.entry.options.get("inject"), fiber.inject)

        # 2. handle self-dispose
        # We only care about `ctx.fiber.dispose()`, so we need to filter out
        # other cases.

        # case 1: fiber is created (uid is set) — dispose 时 uid 已置 None
        if fiber.uid is not None:
            return

        # case 2: fiber is not tracked by loader
        entry = self._entry_of(fiber)
        if entry is None:
            return

        # case 3: fiber is a child plugin under the entry (not the entry's root fiber)
        parent = getattr(fiber.parent, "fiber", None)
        if parent is not None and getattr(parent, "entry", None) is entry:
            return

        # case 4: fiber is disposed on behalf of plugin deletion (such as plugin hmr)
        # self-dispose: ctx.fiber.dispose() -> fiber / runtime dispose -> delete(plugin)
        # plugin hmr: delete(plugin) -> runtime dispose -> fiber dispose
        if not self.ctx.registry.has(fiber.runtime.callback):
            return

        # case 5: the entry's tree is being disposed
        tree_owner = entry.parent.tree.ctx.fiber
        if tree_owner.uid is None or tree_owner.state is FiberState.UNLOADING:
            return

        # case 6: Loader is replacing or removing this exact fiber
        if entry._disposing:
            return

        self.show_log(entry, "unload")

        # case 7: fiber is disposed by loader behavior
        # such as inject checker, config file update, ancestor group disable
        if entry.disabled:
            return

        entry.options["disabled"] = True
        entry.parent.tree.write()

    # --- 服务 API ---

    def show_log(self, entry: Entry, type_: str) -> None:
        if entry.options.get("group") or not entry.parent.tree.enable_logs:
            return
        self.ctx.root.logger("loader").info("%s plugin %s", type_, entry.options.get("name"))

    def locate(self, fiber: Any = None) -> str | None:
        """Return the loader entry id that owns `fiber`, if any."""
        if fiber is None:
            fiber = self.ctx.fiber
        while True:
            entry = self._entry_of(fiber)
            if entry is not None:
                return entry.id
            nxt = getattr(fiber.parent, "fiber", None)
            if fiber is nxt:
                return None
            fiber = nxt

    def exit(self) -> None:
        """Hook for hosts that can restart the process on full-reload requests."""
        pass

    def unwrap_exports(self, exports: Any) -> Any:
        """Normalize ESM/CJS/default export shapes before applying a plugin."""
        if exports is None:
            return exports
        exports = self._take_default(exports)
        # https://github.com/evanw/esbuild/issues/2623
        # https://esbuild.github.io/content-types/#default-interop
        if not getattr(exports, "__esModule", False):
            return exports
        return self._take_default(exports)

    @staticmethod
    def _take_default(exports: Any) -> Any:
        if isinstance(exports, dict):
            default = exports.get("default")
        else:
            default = getattr(exports, "default", None)
        return exports if default is None else default
