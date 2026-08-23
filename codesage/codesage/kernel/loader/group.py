"""EntryGroup — translation of vendor/loader/src/config/group.ts.

条目组:一批子条目的运行时主人。``update`` 事务:
1. ensureId + duplicate id 抛 TypeError
2. 并发 create(TS Promise.allSettled → asyncio.gather return_exceptions)
3. ``ctx.fiber.uid is None`` 时跳过后续("disposal owns termination"):
   sibling 的启动仍在结算,但容器树已走,失败不再描述一次可回滚的更新
4. 失败回滚:新 ids 逆序 remove + 重建旧行;回滚自身失败聚合抛出

``Group`` 插件 = 树载体:把 ``config``(一批 EntryOptions)挂到当前 entry
下;``__cordis_init__``(TS ``[Service.init]``)先 yield ``stop`` 再装载。
树载体插件(Group/Include)的 config 保持字面 —— loader 的 internal/config
靠 ``GROUP`` 牌子识别。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ..utils import AggregateError, GROUP
from .entry import Entry

if TYPE_CHECKING:
    from ..context import Context
    from .tree import EntryTree

__all__ = ["EntryGroup", "Group"]


class EntryGroup:
    key = GROUP

    def __init__(self, ctx: "Context", tree: "EntryTree") -> None:
        self.ctx = ctx
        self.tree = tree
        self.data: list[dict] = []
        entry = getattr(ctx.fiber, "entry", None)
        if entry:
            entry.subgroup = self

    @property
    def context(self) -> "Context":
        return self.ctx

    async def create(self, options: dict) -> str:
        id_ = self.tree.ensure_id(options)
        existing = self.tree.store.get(id_)
        entry = existing if existing is not None else self.tree.store.setdefault(id_, Entry(self.ctx.loader))
        previous_parent = entry.parent
        # Entry may be moved from another group,
        # so we need to update the parent reference.
        entry.parent = self
        # Use `create: true` to replace existing entry.options.
        try:
            await entry.update(options, True, True)
        except BaseException as error:
            if existing is not None:
                entry.parent = previous_parent
            else:
                del self.tree.store[id_]
            raise error from None
        return entry.id

    def unlink(self, options: dict) -> None:
        try:
            self.data.remove(options)
        except ValueError:
            pass

    async def remove(self, id_: str, is_dispose: bool = False) -> None:
        entry = self.tree.store.get(id_)
        if entry is None:
            return
        await entry._dispose()
        if not is_dispose:
            self.unlink(entry.options)
        del self.tree.store[id_]
        self.context.emit("loader/partial-dispose", entry, entry.options, False)

    async def update(self, config: list[dict]) -> None:
        old_config = self.data
        seen: set[str] = set()
        for options in config:
            id_ = self.tree.ensure_id(options)
            if id_ in seen:
                raise TypeError(f"duplicate loader entry id: {id_}")
            seen.add(id_)
        old_map = {o["id"]: o for o in old_config}
        new_map = {o["id"]: o for o in config}

        try:
            outcomes = await asyncio.gather(
                *(self.create(o) for o in config), return_exceptions=True
            )
            # Disposal owns termination: sibling starts can still be settling
            # after the containing tree has gone away, but their failures no
            # longer describe a live update to roll back.
            if self.ctx.fiber.uid is None:
                return
            failures = [o for o in outcomes if isinstance(o, BaseException)]
            if len(failures) == 1:
                raise failures[0]
            if len(failures) > 1:
                raise AggregateError(failures, "loader entries failed to apply")
            for id_ in old_map:
                if id_ not in new_map:
                    await self.remove(id_, True)
            self.data = config
        except BaseException as error:
            rollback_errors: list[Any] = []
            for id_ in reversed(list(new_map)):
                if id_ in old_map:
                    continue
                try:
                    await self.remove(id_, True)
                except BaseException as rollback_error:
                    rollback_errors.append(rollback_error)
            for options in old_config:
                try:
                    await self.create(options)
                except BaseException as rollback_error:
                    rollback_errors.append(rollback_error)
            self.data = old_config
            if rollback_errors:
                raise AggregateError(
                    [error, *rollback_errors], "loader entry rollback failed"
                ) from None
            raise error from None

    async def stop(self) -> None:
        for options in self.data:
            await self.remove(options["id"], True)


class Group(EntryGroup):
    initial: list[dict] = []

    def __init__(self, ctx: "Context", config: list[dict] | None) -> None:
        entry = getattr(ctx.fiber, "entry", None)
        if entry is None:
            raise TypeError("Group plugin requires a parent loader entry")
        super().__init__(ctx, entry.parent.tree)
        # TS `static initial = []` 在 fork 中无人引用;这里兑现其意图:
        # 无 config 的组 → 空子条目(否则 update(None) 崩溃)
        self.config = list(self.initial) if config is None else config
        ctx.on("internal/update", self._on_update)

    def _on_update(self, config: Any, *rest: Any) -> Any:
        return self.update(config)

    async def __cordis_init__(self) -> None:
        yield self.stop
        await self.update(self.config)
