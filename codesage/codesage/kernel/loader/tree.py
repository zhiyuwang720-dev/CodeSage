"""EntryTree — translation of vendor/loader/src/config/tree.ts.

可变的 loader 条目树。持久化由子类实现 ``write()``(Loader 内存树
no-op;Include 落盘)。嵌套组通过 entry 的 ``subtree`` 关联。

``import_module`` 的 ``composeError`` 调用栈整形省略(TS 通过调用栈裁剪
把错误定位到配置行;Python 异常自带 traceback),``get_outer_stack``
参数保留。``internal`` 模块加载器省略(纯 Node),恒 None —— 相对
specifier(``.`` 前缀)相对 ``baseUrl`` 解析为文件路径。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import random
import re
from typing import TYPE_CHECKING, Any, Callable, Iterator

from ..utils import AggregateError
from .entry import Entry
from .group import EntryGroup

if TYPE_CHECKING:
    from ..context import Context


def _import_relative(name: str, base_url: str | None) -> Any:
    """相对 specifier → baseUrl 下的模块文件(TS ``import(new URL(name,
    baseUrl))``;支持目录包与 .py 文件)。"""
    base = base_url or "."
    candidate = os.path.normpath(os.path.join(base, name))
    if os.path.isdir(candidate):
        candidate = os.path.join(candidate, "__init__.py")
    elif not candidate.endswith(".py"):
        candidate = candidate + ".py"
    module_name = "cordis_loader_" + re.sub(r"[^A-Za-z0-9_]", "_", name.strip("./"))
    spec = importlib.util.spec_from_file_location(module_name, candidate)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import loader entry module {name} from {base_url}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError) as error:
        # TS dynamic import 对缺失文件抛模块类错误;文件系统错误包装为
        # ImportError 保持"导入失败"语义(loader 以 'import' 阶段报告)
        raise ImportError(f"cannot import loader entry module {name} from {base_url}") from error
    return module


class EntryTree:
    sep = ":"

    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx.extend({"baseUrl": ctx.baseUrl})
        self.enable_logs: bool | None = None
        self.store: dict[str, Entry] = {}
        self.root = EntryGroup(self.ctx, self)
        entry = getattr(self.ctx.fiber, "entry", None)
        if entry:
            entry.subtree = self

    @property
    def context(self) -> "Context":
        return self.ctx

    def entries(self) -> Iterator[Entry]:
        """Iterate entries in this tree and any nested subtrees."""
        for entry in self.store.values():
            yield entry
            if not entry.subtree:
                continue
            yield from entry.subtree.entries()

    def get_tasks(self) -> list:
        """Return pending import and lifecycle tasks owned by this tree."""
        tasks = []
        for entry in self.entries():
            task = entry._init_task
            if task is None and entry.fiber is not None:
                task = entry.fiber.inertia
            if task is not None:
                tasks.append(task)
        return tasks

    async def await_all(self) -> None:
        """Wait until this tree has no active import or lifecycle tasks.

        Raises a settled fiber failure, or an aggregate when several fibers
        failed.
        """
        while True:
            tasks = self.get_tasks()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                continue
            outcomes = await asyncio.gather(
                *(entry._await() for entry in self.entries()),
                return_exceptions=True,
            )
            failures = [o for o in outcomes if isinstance(o, BaseException)]
            if len(failures) == 1:
                raise failures[0]
            if len(failures) > 1:
                raise AggregateError(failures)
            self.ctx.reflect.notify(["loader"])
            if not self.get_tasks():
                return

    def ensure_id(self, options: dict) -> str:
        if not options.get("id"):
            while True:
                options["id"] = "%08x" % random.getrandbits(32)
                if options["id"] not in self.store:
                    break
        return options["id"]

    def resolve(self, id_: str) -> Entry:
        """Resolve an entry by id, including nested ids separated by sep."""
        parts = id_.split(self.sep)
        tree: "EntryTree | None" = self
        final = parts.pop()
        for part in parts:
            entry = tree.store.get(part)
            tree = entry.subtree if entry is not None else None
            if tree is None:
                raise ValueError(f"cannot resolve entry {id_}")
        entry = tree.store.get(final)
        if entry is None:
            raise ValueError(f"cannot resolve entry {id_}")
        return entry

    def resolve_group(self, id_: str | None) -> EntryGroup:
        if not id_:
            return self.root
        entry = self.resolve(id_)
        if not entry.subgroup:
            raise ValueError(f"entry {id_} is not a group")
        return entry.subgroup

    async def create(self, options: dict, parent: str | None = None, position: int | None = None) -> str:
        """Create an entry in the root group or a nested group."""
        group = self.resolve_group(parent)
        id_ = await group.create(options)
        entry = self.resolve(id_)
        if position is None or position >= len(group.data):
            group.data.append(entry.options)
        else:
            group.data.insert(position, entry.options)
        group.tree.write()
        return id_

    async def remove(self, id_: str) -> None:
        """Stop and remove an entry from its parent group."""
        entry = self.resolve(id_)
        await entry.parent.remove(id_)
        entry.parent.tree.write()

    async def update(self, id_: str, options: dict, parent: str | None = None, position: int | None = None) -> None:
        """Update an entry and optionally move it to another group."""
        entry = self.resolve(id_)
        source = entry.parent
        try:
            source_index = source.data.index(entry.options)
        except ValueError:
            source_index = -1
        target = source
        if parent is not None:
            target = self.resolve_group(parent)
            source.unlink(entry.options)
            if position is None or position >= len(target.data):
                target.data.append(entry.options)
            else:
                target.data.insert(position, entry.options)
            entry.parent = target
        try:
            await entry.update(options, False, True)
        except BaseException as error:
            if parent is not None:
                target.unlink(entry.options)
                if source_index < 0:
                    source.data.append(entry.options)
                else:
                    source.data.insert(source_index, entry.options)
                entry.parent = source
                try:
                    await entry.update({}, False, True)
                except BaseException as rollback_error:
                    raise AggregateError(
                        [error, rollback_error], f"failed to roll back loader entry move {id_}"
                    ) from None
            raise error from None
        source.tree.write()
        if target is not source:
            target.tree.write()

    async def import_module(self, name: str, get_outer_stack: Callable[[], list] | None = None) -> Any:
        """Import a plugin module from a specifier or `cordis:` builtin."""
        if name.startswith("cordis:"):
            return self.ctx.loader.builtins.get(name[7:])
        if self.ctx.loader.internal is not None:
            return self.ctx.loader.internal.import_(name, self.ctx.baseUrl, {})
        if name.startswith("."):
            return _import_relative(name, self.ctx.baseUrl)
        return importlib.import_module(name)

    def write(self) -> None:
        """Persist current tree state. In-memory trees may no-op."""
        raise NotImplementedError
