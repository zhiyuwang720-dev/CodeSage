"""Include — translation of vendor/include/src/index.ts.

文件后备的 loader 子树:YAML/JSON 配置文件 → entry 列表;patches 运行时
覆盖;HMR(文件变更 → 重读重应用);原子写(tmp + rename,重试 backoff
``(retry+1)*50ms``)带 debounce(下一拍合并)。

与 TS 的差异:
- js-yaml 的 JsExpr Type → PyYAML 自定义 Loader/Dumper(``tag:yaml.org,
  2002:js`` ↔ ``{"__jsExpr": str}`` 往返);``JSON_SCHEMA`` 等价 = SafeLoader
  去掉 YAML 1.1 扩展 implicit resolvers,只留 JSON 核心类型
- ``node:url`` 的 URL 解析 → 路径字符串(normpath/join;绝对路径直用)
- ``import()`` 模块分支不可达(构造已校验扩展名)省略
- ``Service.init`` 的 yield-stop 形态照 group.py 的写法(async generator)
- ``%C`` 日志占位 → ``%s``(Python printf 风格)
"""

from __future__ import annotations

import asyncio
import copy
import errno
import inspect
import json
import os
import re
from typing import TYPE_CHECKING, Any, Callable

import yaml

from .loader import EntryGroup, EntryTree

if TYPE_CHECKING:
    from .context import Context

__all__ = [
    "ConfigFileError",
    "Include",
    "apply_entry_patches",
    "entry_list_schema",
]

#: YAML ``!!js`` 标签(TS ``new yaml.Type('tag:yaml.org,2002:js', ...)``)
JS_EXPR_TAG = "tag:yaml.org,2002:js"

WRITE_RETRY_LIMIT = 10
WRITE_RETRY_DELAY_MS = 50


def _is_js_expr(value: Any) -> bool:
    return isinstance(value, dict) and "__jsExpr" in value


# --- YAML 方言(js-yaml JSON_SCHEMA.extend(JsExpr) 的 PyYAML 等价) ---


def _js_expr_construct(loader: Any, node: Any) -> dict:
    return {"__jsExpr": loader.construct_scalar(node)}


class _EntryListLoader(yaml.SafeLoader):
    """JSON_SCHEMA 等价:JSON 核心类型 + ``!!js`` 标签,无 YAML 1.1 扩展。

    js-yaml 的 JsExpr resolve 只对显式 ``!!js`` 的 scalar 生效(未打 tag 的
    标量按 schema 顺序命中 core 类型)—— PyYAML 侧 add_constructor 即等价;
    implicit resolvers 清空后只剩下面四个 JSON 核心(时间戳、yes/no/on/off、
    hex/octal 等 1.1 扩展全部移除),未匹配文本按 PyYAML 规则落到 str。
    """


_EntryListLoader.add_constructor(JS_EXPR_TAG, _js_expr_construct)
_EntryListLoader.yaml_implicit_resolvers = {}
_EntryListLoader.add_implicit_resolver(
    "tag:yaml.org,2002:null",
    re.compile(r"^(?:~|null|Null|NULL|)$"),
    ["~", "n", "N", ""],
)
_EntryListLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)
_EntryListLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^(?:[-+]?[0-9]+)$"),
    list("-+0123456789"),
)
_EntryListLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(
        r"^(?:[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?[0-9]+[eE][-+]?[0-9]+)$"
    ),
    list("-+0123456789."),
)


class _EntryListDumper(yaml.SafeDumper):
    """``!!js`` 往返:``{"__jsExpr": str}`` → 标量;其余按 JSON 风格输出。"""

    def represent_dict(self, data: dict) -> Any:
        if _is_js_expr(data):
            return self.represent_scalar(JS_EXPR_TAG, str(data["__jsExpr"]))
        return super().represent_dict(data)


_EntryListDumper.add_representer(dict, _EntryListDumper.represent_dict)


#: 入口列表 YAML 方言的 load 端(TS ``entryListSchema = JSON_SCHEMA.extend(JsExpr)``)
entry_list_schema = _EntryListLoader


# --- patches ---


def apply_entry_patches(
    data: list[dict],
    patches: list[dict] | None,
    warn: Callable[[str, ...], None],
) -> list[dict]:
    """Apply patch lists to an entry list — THE patch semantics of this include.

    输入永不修改、结果与输入完全脱离(structuredClone → deepcopy):
    反复应用(配置热更)可正确还原被移除/变更的 patch。insert 的行立即进入
    索引,同列表后续 patch 可定位它。匹配不到目标的 patch 警告并跳过。
    """
    data = copy.deepcopy(data)
    if not patches:
        return data

    entry_map: dict[str, dict] = {}

    def build_map(entries: list[dict]) -> None:
        for entry in entries:
            if entry.get("id"):
                entry_map[entry["id"]] = entry
            if entry.get("group") and isinstance(entry.get("config"), list):
                build_map(entry["config"])

    build_map(data)

    for patch in patches:
        pid = patch.get("id")
        insert = patch.get("insert")
        name = patch.get("name")
        overrides = {k: v for k, v in patch.items() if k not in ("id", "insert", "name")}

        # TS `if (insert)` 对空数组同样为真(JS truthy)—— 用 is not None
        if insert is not None:
            if pid:
                target = entry_map.get(pid)
                if not target:
                    warn("patch insert: entry %C not found", pid)
                    continue
                if not target.get("group"):
                    warn("patch insert: entry %C is not a group", pid)
                    continue
                if not isinstance(target.get("config"), list):
                    target["config"] = []
                target["config"].extend(insert)
            else:
                data.extend(insert)
            # Index what this patch added so a LATER patch in the same list can
            # target it.
            build_map(insert)
            continue

        if not pid:
            warn("patch: id is required for non-insert patches")
            continue

        target = entry_map.get(pid)
        if not target:
            warn("patch: entry %C not found", pid)
            continue

        if name and name != target.get("name"):
            warn(
                "patch: name mismatch for %C (expected %C, got %C), skipping",
                pid,
                target.get("name"),
                name,
            )
            continue

        for key, value in overrides.items():
            if key == "id":
                continue
            target[key] = value

    return data


# --- Include ---


class ConfigFileError(RuntimeError):
    def __init__(self, stage: str, path: str, cause: Any = None) -> None:
        self.stage = stage
        self.path = path
        self.cause = cause
        super().__init__(f"failed to {stage} config file {path}")


WRITABLE = {
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


class Include(EntryTree):
    inject = ["loader"]

    # Tree-carrier marker (the Group plugin declares the same): this config is
    # entry and patch lists, so the Loader's `internal/config` interpolation
    # keeps it literal — a `!!js` expression inside a nested row's config
    # belongs to that row's fiber, resolving lazily in the row's own context.
    # Include's own fields (`path`, `enableLogs`) therefore stay literal too.
    key = EntryGroup.key

    def __init__(self, ctx: "Context", config: dict) -> None:
        super().__init__(ctx)
        enable = config.get("enableLogs")
        if enable is None:
            entry = getattr(ctx.fiber, "entry", None)
            parent = getattr(entry, "parent", None)
            tree = getattr(parent, "tree", None) if parent is not None else None
            enable = getattr(tree, "enable_logs", None) if tree is not None else None
        self.enable_logs = enable if enable is not None else False

        self.config = config
        path = config["path"]
        base = self.ctx.baseUrl or "."
        self.filename = (
            os.path.normpath(os.path.join(base, path))
            if not os.path.isabs(path)
            else path
        )
        ext = os.path.splitext(self.filename)[1]
        if ext not in WRITABLE:
            raise ValueError(f'extension "{ext}" not supported')
        self.type = WRITABLE[ext]
        self.readonly = False
        # TS `this.ctx.baseUrl = new URL('.', pathToFileURL(filename)).href`
        object.__setattr__(ctx, "baseUrl", os.path.dirname(self.filename))

        self.content: str | None = None
        self.data: list[dict] | None = None
        self.write_task: Any = None
        self.pending_write: list[dict] | None = None
        self.write_queue: asyncio.Task | None = None
        self.apply_queue: asyncio.Task | None = None

        ctx.on("internal/update", self._on_internal_update)

    # --- HMR ---

    async def _on_internal_update(self, config: dict, no_save: Any, next_fn: Callable) -> Any:
        if config.get("path") != self.config.get("path"):
            # TS `return next()` 的 Promise 被 async 函数自动展平;Python
            # 协程作为返回值会被丢弃,须显式 await 链结果
            result = next_fn()
            if result is not None and inspect.isawaitable(result):
                return await result
            return result
        await self.enqueue(lambda: self._reapply(config))
        return None

    async def _reapply(self, config: dict) -> None:
        data = self.apply_patches(self.data, config.get("patches"))
        await self.root.update(data)
        self.config = config

    # --- 队列 ---

    def enqueue(self, task: Callable[[], Any]) -> asyncio.Task:
        """Serial one child-tree mutation behind every earlier one.

        The group's transactional ``update`` is not reentrant: two concurrent
        applies interleave create and rollback on the same entries. A
        predecessor's failure is its own caller's outcome and never gates the
        next task (TS ``applyQueue = run.then(noop)``).
        """
        prev = self.apply_queue

        async def run() -> Any:
            if prev is not None:
                try:
                    await prev
                except BaseException:
                    pass
            return await task()

        self.apply_queue = asyncio.ensure_future(run())
        return self.apply_queue

    # --- 读 ---

    async def check_access(self) -> None:
        if not self.type:
            return
        if os.access(self.filename, os.W_OK):
            return
        self.readonly = True

    async def read(self, forced: bool = False) -> dict | None:
        try:
            with open(self.filename, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as error:
            raise ConfigFileError("read", self.filename, error) from error
        if not forced and self.content == content:
            return None
        try:
            if self.type == "application/yaml":
                data = yaml.load(content, Loader=_EntryListLoader)
            elif self.type == "application/json":
                data = json.loads(content)
            else:
                raise AssertionError(
                    "unreachable: extension validated in constructor (TS import() branch)"
                )
        except Exception as error:
            raise ConfigFileError("parse", self.filename, error) from error
        if not isinstance(data, list):
            raise ConfigFileError(
                "validate",
                self.filename,
                TypeError("config file must be a top-level array"),
            ) from None
        return {"content": content, "data": data}

    def apply_patches(self, data: list[dict], patches: list[dict] | None) -> list[dict]:
        return apply_entry_patches(
            data,
            patches,
            lambda message, *args: self.ctx.root.logger("loader").warn(
                message.replace("%C", "%s"), *args
            ),
        )

    # --- 生命周期 ---

    async def __cordis_init__(self) -> Any:
        try:
            candidate = await self.read(True)
        except ConfigFileError as error:
            if (
                error.stage != "read"
                or not isinstance(error.cause, OSError)
                or error.cause.errno != errno.ENOENT
            ):
                raise
            if self.config.get("initial"):
                await self._write_file(self.config["initial"])
                candidate = await self.read(True)
            else:
                raise FileNotFoundError(
                    f"config file not found: {self.filename}"
                ) from error

        yield self.stop
        await self.apply(candidate)

    async def stop(self) -> None:
        await self.root.stop()
        await self.flush_write()

    async def refresh(self) -> None:
        """Re-read the file and transactionally refresh child entries."""
        async def inner() -> None:
            candidate = await self.read()
            if candidate is None:
                return
            await self._apply(candidate)

        await self.enqueue(inner)

    def apply(self, candidate: dict) -> asyncio.Task:
        return self.enqueue(lambda: self._apply(candidate))

    async def _apply(self, candidate: dict) -> None:
        data = self.apply_patches(candidate["data"], self.config.get("patches"))
        await self.root.update(data)
        self.content = candidate["content"]
        self.data = candidate["data"]
        await self.check_access()

    # --- 写 ---

    async def _write_file(self, config: list[dict]) -> None:
        if self.readonly:
            raise ValueError("cannot overwrite readonly config")
        if self.type == "application/yaml":
            self.content = yaml.dump(
                config, Dumper=_EntryListDumper, sort_keys=False, allow_unicode=True
            )
        elif self.type == "application/json":
            self.content = json.dumps(config, indent=2)
        else:
            raise AssertionError("unreachable")
        with open(self.filename + ".tmp", "w", encoding="utf-8") as f:
            f.write(self.content)
        for retry in range(WRITE_RETRY_LIMIT + 1):
            try:
                os.replace(self.filename + ".tmp", self.filename)
                return
            except OSError as error:
                if (
                    error.errno not in (errno.EACCES, errno.EBUSY, errno.EPERM)
                    or retry >= WRITE_RETRY_LIMIT
                ):
                    raise
                await asyncio.sleep((retry + 1) * WRITE_RETRY_DELAY_MS / 1000)

    def write_file(self, config: list[dict]) -> Any:
        """Schedule a write on the next event-loop tick (debounce 合并)。"""
        if self.write_task is not None:
            self.write_task.cancel()
        self.pending_write = config
        loop = asyncio.get_running_loop()
        self.write_task = loop.call_soon(lambda: loop.create_task(self.flush_write()))
        return None

    async def flush_write(self) -> Any:
        if self.write_task is not None:
            self.write_task.cancel()
            self.write_task = None
        config = self.pending_write
        self.pending_write = None
        if config is None:
            return self.write_queue
        prev = self.write_queue

        async def run() -> None:
            if prev is not None:
                try:
                    await prev
                except BaseException:
                    pass
            await self._write_file(config)

        task = asyncio.ensure_future(run())
        # TS `writeQueue = run.catch(noop)` —— 队列吞错;run 的错误归调用者
        async def drain() -> None:
            try:
                await task
            except BaseException:
                pass

        self.write_queue = asyncio.ensure_future(drain())

        def on_error(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            self.ctx.root.logger("loader").warn(
                f"failed to write config file {self.filename}"
            )
            self.ctx.root.logger("loader").warn(str(t.exception()))

        task.add_done_callback(on_error)
        return task

    def write(self) -> Any:
        self.context.emit("loader/config-update")
        return self.write_file(self.root.data)
