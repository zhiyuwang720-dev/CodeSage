"""LoggerService tests — cordis logger.ts 翻译验证:printf/exporter/级别。"""

from codesage.kernel import Context, Logger, Message
from codesage.kernel.logger import INFO, WARN
from codesage.kernel.utils import INTERCEPT, AggregateError


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_named_logger_message_meta():
    ctx = Context()
    logger = ctx.logger("my")
    logger.info("hi")
    (message,) = ctx.logger.buffer
    assert isinstance(message, Message)
    assert message.sn == 1
    assert message.name == "my"
    assert message.type == "info"
    assert message.level == INFO
    assert message.args == ["hi"]


def test_default_name_from_fiber():
    ctx = Context()
    logger = ctx.logger()
    assert logger.name == "root"  # root fiber 名


def test_facade_uses_fiber_name():
    ctx = Context()
    ctx.logger.error("boom")
    assert ctx.logger.buffer[0].name == "root"
    assert ctx.logger.buffer[0].type == "error"


def test_printf_formatting():
    ctx = Context()
    logger = ctx.logger("my")
    logger.info("hello %s, %d, %.1f", "world", 3, 2.5)
    message = ctx.logger.buffer[-1]
    exporter = {"formatters": None, "colors": 0, "maxLength": 10240}
    # %.1f 不是 TS 的 %([a-zA-Z%]) 格式符 → 原样保留;2.5 未被消费 → 尾随追加
    assert Logger.format(exporter, message) == "hello world, 3, %.1f 2.5"


def test_error_expansion():
    ctx = Context()
    ctx.logger("my").error(ValueError("boom"))
    message = ctx.logger.buffer[-1]
    assert message.type == "error"
    assert isinstance(message.args[0], ValueError)  # 展开发生在 format() 时(TS 同款)
    assert Logger.format({}, message) == "boom"


def test_aggregate_error_expansion():
    ctx = Context()
    ctx.logger("my").error(AggregateError([ValueError("a"), ValueError("b")]))
    assert [str(m.args[0]) for m in ctx.logger.buffer[-2:]] == ["a", "b"]


def test_object_argument_uses_o_formatter():
    ctx = Context()
    ctx.logger("my").info({"a": 1})
    message = ctx.logger.buffer[-1]
    exporter = {"formatters": None, "colors": 0, "maxLength": 10240}
    assert Logger.format(exporter, message) == '{"a":1}'  # TS JSON.stringify 无空格


def test_levels_filter_skips_above_threshold():
    seen = []

    ctx = Context()
    ctx.logger.exporter({"colors": 0, "levels": {"default": WARN}, "export": lambda m: seen.append(m)})
    ctx.logger("my").info("i")
    ctx.logger("my").warn("w")
    ctx.logger("my").debug("d")
    assert [m.args[0] for m in seen] == ["i", "w"]  # DEBUG 被阈值过滤


def test_exporter_object_form():
    seen = []

    class exporter:
        colors = 0

        def export(self, message):
            seen.append(message)

    ctx = Context()
    ctx.logger.exporter(exporter())
    ctx.logger("my").info("x")
    assert seen[0].args == ["x"]


def test_exporter_disposer_stops():
    seen = []

    async def run():
        ctx = Context()
        dispose = ctx.logger.exporter({"colors": 0, "export": lambda m: seen.append(m)})
        ctx.logger("my").info("x")
        assert len(seen) == 1
        await dispose()
        ctx.logger("my").info("y")
        assert len(seen) == 1

    _run(run())


def test_intercept_config_level():
    """intercept 的 level 作为默认导出阈值:超阈值(DEBUG)被过滤。"""
    ctx = Context()
    getattr(ctx, INTERCEPT)["logger"] = {"level": WARN}
    seen = []

    ctx.logger.exporter({"colors": 0, "export": lambda m: seen.append(m)})
    ctx.logger("my").info("i")
    ctx.logger("my").warn("w")
    ctx.logger("my").debug("d")
    assert [m.args[0] for m in seen] == ["i", "w"]
