"""Loader + Patch — 阶段 21 最小 Loader(cordis-plugin-loader 语义子集)。

manifest 行装载 + patch 应用(阶段 22 Profile/Bundle 的地基,见 specs/21):
- 行 = {id, name, config?, disabled?, inject?};disabled 行跳过
- mount():逐行装载;激活序由 kernel 的 inject 机制推导(依赖缺失 →
  PENDING,就绪 → ACTIVE,即拓扑序)
- apply_patches():按 id 定位,整行 config 替换(last-wins);新 id 插入新行
- 配置插值:仅字面量 + ``$env:`` 取值(``!!js`` 表达式 DSL 留阶段 22)

与 cordis-plugin-loader 的差异(最小等价,阶段 22 扩):
- 无 import 机制:name → 插件由构造传入的 plugins 字典解析
- 无 EntryGroup/EntryTree 持久化、isolate/intercept 选项、HMR;
  patch 改 name 不换插件(整行替换语义留阶段 22)
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Iterator

from .fiber import Fiber
from .registry import resolve_inject

if TYPE_CHECKING:
    from .context import Context


def _interpolate(value: Any) -> Any:
    """递归替换 ``$env:NAME`` → 环境变量(仅字面量 + $env:)。"""
    if isinstance(value, str):
        return os.environ.get(value[5:], "") if value.startswith("$env:") else value
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


class Loader:
    """manifest 行装载器:行 → Fiber;按 id patch,config last-wins。"""

    def __init__(
        self,
        ctx: "Context",
        manifest: list[dict],
        plugins: dict[str, Any] | None = None,
    ) -> None:
        self.ctx = ctx
        self.plugins: dict[str, Any] = plugins or {}
        self._rows: dict[str, dict] = {}
        self._fibers: dict[str, Fiber] = {}
        for row in manifest:
            self._rows[row["id"]] = dict(row)

    @property
    def rows(self) -> Iterator[dict]:
        return iter(self._rows.values())

    # --- 装载 ---

    def mount(self) -> None:
        """装载全部非 disabled 行(manifest 序创建;激活序 = inject 拓扑)。"""
        for row in list(self._rows.values()):
            self._mount_row(row)

    def _mount_row(self, row: dict) -> None:
        if row.get("disabled"):
            return
        plugin = self.plugins[row["name"]]
        config = _interpolate(row.get("config"))
        inject = row.get("inject")
        if inject:
            # 行级 inject 声明 → apply 对象形状(kernel registry 同款)
            plugin = {"inject": inject, "apply": plugin, "name": row["name"]}
        self._fibers[row["id"]] = self.ctx.plugin(plugin, config)

    # --- patch ---

    async def apply_patches(self, patches: list[dict]) -> None:
        """按 id 定位,整行替换 config;同 id 多次出现 last-wins;新 id 插入。
        改 disabled:停用行 → 卸载 fiber;启用行 → 装载并等待。"""
        for patch in patches:
            id_ = patch["id"]
            row = self._rows.get(id_)
            if row is None:
                row = self._rows[id_] = {}
            row.update(patch)

            fiber = self._fibers.get(id_)
            if row.get("disabled"):
                if fiber is not None:
                    await fiber.dispose()
                    del self._fibers[id_]
                continue
            if fiber is None:
                self._mount_row(row)
                await self._fibers[id_].wait()
            else:
                # 非 ACTIVE(PENDING/UNLOADING)纤维 update 直接改配置不重启
                result = fiber.update(_interpolate(row.get("config")))
                if result is not None:
                    await result
