"""LoggerService — translation of vendor/cordis/src/logger.ts.

结构化日志服务(ctx.logger):
- ``ctx.logger(name)`` → 命名 Logger(可调用服务);``ctx.logger.error(...)``
  直接用当前 fiber 派生名
- Logger:printf 格式(format 用默认/自定义 formatter),Error 参数自动展开
  为 stack,对象参数走 %o
- exporter:注册即成为消息接收端(buffer 是内置 exporter,1000 条环形)

Python 映射差异:
- Message.fiber 是 TS 的 WeakRef<Fiber> 仅用于 GC 诊断 → 省略
- _resolveConfig:TS 沿 intercept 原型链收集,Python 写时复制 dict 已是
  快照(祖先条目已并入),单层读取即可
- createCallable 包装 → Python 类直接实现 ``__call__``
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .utils import AggregateError, INTERCEPT

if TYPE_CHECKING:
    from .context import Context

#: 日志严重级别(TS LoggerLevel)
ERROR = 0
INFO = 1
WARN = 2
DEBUG = 3

#: ANSI 调色板(TS c16 / c256)
c16 = [6, 2, 3, 4, 5, 1]
c256 = [
    20, 21, 26, 27, 32, 33, 38, 39, 40, 41, 42, 43, 44, 45, 56, 57, 62,
    63, 68, 69, 74, 75, 76, 77, 78, 79, 80, 81, 92, 93, 98, 99, 112, 113,
    129, 134, 135, 148, 149, 160, 161, 162, 163, 164, 165, 166, 167, 168,
    169, 170, 171, 172, 173, 178, 179, 184, 185, 196, 197, 198, 199, 200,
    201, 202, 203, 204, 205, 206, 207, 208, 209, 214, 215, 220, 221,
]


@dataclass(slots=True)
class Message:
    """结构化日志记录(TS Message)。"""

    sn: int
    ts: float
    name: str
    type: str
    level: int
    args: list[Any] = field(default_factory=list)


#: 内置占位符 formatter(TS defaultFormatters)
def _f_str(value: Any, exporter: Any, message: Message) -> str:
    return str(value)


def _f_int(value: Any, exporter: Any, message: Message) -> str:
    return str(int(value))


def _f_float(value: Any, exporter: Any, message: Message) -> str:
    return str(float(value))


def _f_json(value: Any, exporter: Any, message: Message) -> str:
    return json.dumps(value, separators=(",", ":"))  # TS JSON.stringify 无空格


def _f_empty(value: Any, exporter: Any, message: Message) -> str:
    return ""


def _f_color(value: Any, exporter: Any, message: Message) -> str:
    return Logger.color(exporter, Logger.code(message.name, exporter.colors), value)


default_formatters: dict[str, Any] = {
    "s": _f_str,
    "d": _f_int,
    "i": _f_int,
    "f": _f_float,
    "o": _f_json,
    "O": _f_json,
    "c": _f_empty,
    "C": _f_color,
}


def _hyphenate(value: str) -> str:
    """camelCase → kebab-case(TS cosmokit hyphenate)。"""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value).lower()


def _exp_attr(exporter: Any, name: str, default: Any = None) -> Any:
    """exporter 双形态读取:dict(便捷)或对象属性(TS 接口形态)。"""
    if isinstance(exporter, dict):
        return exporter.get(name, default)
    return getattr(exporter, name, default)


class Logger:
    """单一日志器门面(TS Logger):四个严重级别方法 + printf 格式化。"""

    @staticmethod
    def color(exporter: Any, code: int, value: Any, decoration: str = "") -> str:
        if not _exp_attr(exporter, "colors"):
            return "" + str(value)
        head = f"3{code}" if code < 8 else f"38;5;{code}"
        return f"\x1b[{head}{decoration}m{value}\x1b[0m"

    @staticmethod
    def code(name: str, level: int | bool | None = None) -> int:
        """日志器名 → 调色板下标(TS 同款散列)。"""
        hash_ = 0
        for char in name:
            hash_ = ((hash_ << 3) - hash_) + ord(char) + 13
            hash_ &= 0xFFFFFFFF  # TS |= 0 的 int32 截断
            if hash_ >= 0x80000000:
                hash_ -= 0x100000000
        colors = [] if not level else (c256 if level >= 2 else c16)
        return colors[abs(hash_) % len(colors)] if colors else 0

    @staticmethod
    def format(exporter: Any, message: Message) -> str:
        """printf 格式化(TS Logger.format)。"""
        args = list(message.args)
        if args and isinstance(args[0], Exception):
            # Error 参数 → 输出消息(TS 输出 .stack,Python 无等价栈)
            args[0] = str(args[0])
            args.insert(0, "%s")
        elif not args or not isinstance(args[0], str):
            args.insert(0, "%o")

        fmt: str = args.pop(0)

        def repl(match: Any) -> str:
            char = match.group(1)
            if match.group(0) == "%%":
                return "%"
            formatter = (_exp_attr(exporter, "formatters") or {}).get(char) or default_formatters.get(char)
            if callable(formatter):
                value = args.pop(0)
                return str(formatter(value, exporter, message))
            return match.group(0)

        fmt = re.sub(r"%([a-zA-Z%])", repl, fmt)

        o_formatter = (_exp_attr(exporter, "formatters") or {}).get("o") or default_formatters["o"]
        for arg in args:
            if isinstance(arg, (dict, list, tuple)):  # TS typeof === 'object'
                arg = o_formatter(arg, exporter, message)
            fmt += " " + str(arg)

        max_length = _exp_attr(exporter, "maxLength") or 10240
        return "\n".join(
            line[:max_length] + ("..." if len(line) > max_length else "")
            for line in fmt.split("\n")
        )

    def __init__(self, name: str, service: "LoggerService", level: int | None = None, meta: dict | None = None) -> None:
        self.name = name
        self.service = service
        self.level = level
        self.meta = meta or {}
        self.error = self._method("error", ERROR)
        self.info = self._method("info", INFO)
        self.warn = self._method("warn", WARN)
        self.debug = self._method("debug", DEBUG)

    def _method(self, type_: str, level: int):
        def method(*args: Any) -> None:
            # 单个 Error 参数:展开 cause / AggregateError(TS 同款)
            if len(args) == 1 and isinstance(args[0], Exception):
                cause = getattr(args[0], "__cause__", None)
                if cause:
                    method(cause)
                    return
                if isinstance(args[0], AggregateError) and args[0].errors:
                    for e in args[0].errors:
                        method(e)
                    return
            sn = self.service._sn_message + 1
            self.service._sn_message = sn
            ts = time.time()
            for exporter in self.service.exporters.values():
                levels = _exp_attr(exporter, "levels") or {}
                target = levels.get(self.name, levels.get("default", self.level if self.level is not None else INFO))
                if target < level:
                    continue
                _exp_attr(exporter, "export")(Message(sn=sn, ts=ts, type=type_, level=level, name=self.name, args=list(args)))

        return method


class LoggerService:
    """可调用日志服务:``ctx.logger()`` 派生当前 fiber 命名的日志器。"""

    buffer_size = 1000
    _sn_message = 0
    _sn_exporter = 0

    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx
        self.buffer: list[Message] = []
        self.exporters: dict[int, Any] = {}

        # 内置环形 buffer exporter(TS 构造器同款)
        self.exporter({"colors": 3, "export": lambda message: self._buffer_export(message)})

    def _buffer_export(self, message: Message) -> None:
        self.buffer.append(message)
        if len(self.buffer) > self.buffer_size:
            self.buffer = self.buffer[-self.buffer_size:]

    def exporter(self, exporter: Any):
        """注册 exporter(fiber effect,卸载自动移除);返回注销 disposer。"""
        return self.ctx.effect(
            lambda: self._exporter_impl(exporter),
            "ctx.logger.exporter()",
        )

    def _exporter_impl(self, exporter: Any):
        self._sn_exporter += 1
        key = self._sn_exporter
        self.exporters[key] = exporter
        return lambda: self.exporters.pop(key, None)

    def _resolve_config(self) -> dict:
        intercept = getattr(self.ctx, INTERCEPT)
        config = intercept.get("logger") or {}
        return dict(config)

    def __call__(self, name: str | None = None) -> Logger:
        """``ctx.logger(name)``:无 name 用 intercept 配置或 fiber 名派生。"""
        config = self._resolve_config()
        name = name or config.get("name")
        if not name:
            name = _hyphenate(self.ctx.fiber.name)
        return Logger(name, self, level=config.get("level"))

    # ctx.logger.error(...) → 当前 fiber 派生日志器直接记录(TS prototype 同款)
    def error(self, *args: Any) -> None:
        return self().error(*args)

    def info(self, *args: Any) -> None:
        return self().info(*args)

    def warn(self, *args: Any) -> None:
        return self().warn(*args)

    def debug(self, *args: Any) -> None:
        return self().debug(*args)
