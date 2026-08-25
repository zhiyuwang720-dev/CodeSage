"""tools 注册表与执行管线的契约面(参考实现 tools/index.ts 契约部分实现)。

本模块是 agent-loop 等消费方看到的完整契约,不依赖注册表实现:

- 执行对象词汇:``ToolExecutionInput``(调用方给)→ ``ToolExecution``
  (注册表补 token/rootCallId 的管道对象)→ ``ToolRunContext``(工具
  本体拿到的运行时上下文);
- 调度器协议 ``ToolRuntimeScheduler``:``prepare``(有序 pre-execute/
  guard 门)→ ``dispatch``(around-dispatch/本体)→ ``finalize``/
  ``finish``(post-execute 与内容定稿);
- 失败词汇:``TOOL_ABORTED`` / ``TOOL_ABORTED_BEFORE_DISPATCH``
  两个规范错误码,``ToolNotFoundError`` / ``ToolOutputError`` 两个
  HarnessError 子类(路由按 code 走);
- ``ToolRuntime`` 服务骨架:空注册表的 fail-closed 语义 —— 任何
  调用都判 exclusive、任何派发都是 UNKNOWN_TOOL。注册表本体
  (register/schemas/execute/三层瀑布)在后续批次实现,届时替换
  scheduler 槽与 executionMode 的查找面,契约不动。

**Python 实现差异**(均在注释中标出):

- 参考实现 的 unique symbol 属性 → 模块级哨兵 + ``__getitem__`` 槽;
- 无 schemastery/zod:Config 手写默认与校验;
- 类型以 TypedDict/Protocol 表达,运行时不强制(契约文档化)。
"""

from __future__ import annotations

from typing import Awaitable, Callable, Literal, Protocol, TypedDict

from cordis import Service

from llm.llm.src.error_chain import HarnessError

__all__ = [
    "Config",
    "CodeDispatchLog",
    "PreToolDecision",
    "PostToolDecision",
    "ScheduledToolDispatch",
    "ScheduledToolPreparation",
    "TOOL_ABORTED",
    "TOOL_ABORTED_BEFORE_DISPATCH",
    "TOOL_RUNTIME_SCHEDULER",
    "ToolDefinition",
    "ToolDispatchExecution",
    "ToolErrorInfo",
    "ToolExecution",
    "ToolExecutionFailure",
    "ToolExecutionInput",
    "ToolExecutionMode",
    "ToolExecutionResult",
    "ToolExecutionSuccess",
    "ToolFailure",
    "ToolNotFoundError",
    "ToolOutputDefinition",
    "ToolOutputError",
    "ToolPresentationMode",
    "ToolRestriction",
    "ToolResult",
    "ToolRunContext",
    "ToolRuntime",
    "ToolRuntimeScheduler",
    "error_message",
    "failure_message_from_content",
    "tool_error_result",
]

#: 调度器入口的哨兵键:参考实现 是 unique symbol 实例属性,Python 用
#: 模块级 object 哨兵 + ToolRuntime.__getitem__ 保持同形访问
#: (``ctx.tools[TOOL_RUNTIME_SCHEDULER]``)。它不在生成的服务 API 里。
TOOL_RUNTIME_SCHEDULER = object()

#: 规范错误码:工具本体已被调用之后到来的取消。
TOOL_ABORTED = "ABORTED"

#: 规范错误码:工具本体被调用之前到来的取消(模型侧请求作废)。
TOOL_ABORTED_BEFORE_DISPATCH = "ABORTED_BEFORE_DISPATCH"


# ---- 执行对象词汇 ----


class ToolExecutionInput(TypedDict):
    """调用方对一次工具调用的描述;注册表补 token 后成为管道对象。"""

    callId: str
    #: 根模型请求调用;根执行省略,嵌套派发传播外层值。
    rootCallId: str | None
    name: str
    #: 已无损 JSON 化的解析参数(工具自己校验自己的 schema)。
    arguments: object
    #: 该调用为之运行的 agent(agent loop 设定;作用域路由键)。
    agent: object | None
    #: 外层传输执行的令牌(Code Mode SDK 子派发用);带 parent 的
    #: 调用被视为传输子派发,不受 code 折叠约束。
    parent: object | None
    #: 调用方持有的取消信号(AbortSignal 的 Python 侧承交)。
    signal: object


class ToolExecutionInputNoSignal(TypedDict):
    callId: str
    rootCallId: str | None
    name: str
    arguments: object
    agent: object | None
    parent: object | None


class ToolExecution(ToolExecutionInput):
    """注册表补全后的执行对象:token + 解析后的根调用 id。"""

    rootCallId: str
    #: 注册表分配的标识;对外只作为不透明 parent 令牌。
    token: object


class ToolDispatchExecution(ToolExecutionInputNoSignal):
    """around-dispatch 包装视角:signal 可替换但不可移除。"""

    signal: object


#: 一次待发调度的调度模式:parallel 可与兄弟重叠,exclusive 独跑
#: 并形成排序屏障。
class ToolExecutionMode(TypedDict):
    kind: Literal["parallel", "exclusive"]


class CodeDispatchLog(TypedDict):
    """一次已结算的 run_code 子派发(进入 code-dispatch-log 瀑布)。"""

    exec: dict
    agent: object | None
    subCallId: str
    name: str
    isError: bool
    content: list


class ToolRunContext(Protocol):
    """工具本体在注册表接受后拿到的运行时上下文。"""

    callId: str
    rootCallId: str
    name: str
    arguments: object
    signal: object

    def deferContext(self, context) -> None:
        """把一条上下文推迟到本工具最终结果到达 agent loop 时。"""
        ...

    def concludeTurn(self) -> None:
        """把一次成功终局标记为当前 agent 回合的终结点。"""
        ...


#: 调度器私有视图:pre/post 策略有序,派发可重叠。不是插件扩展点。
class ScheduledToolPreparation(TypedDict):
    kind: Literal["dispatch", "post-result", "final-result"]
    exec: ToolRunContext
    result: dict | None


class ScheduledToolDispatch(TypedDict):
    kind: Literal["post-result", "final-result"]
    result: dict


class ToolRuntimeScheduler(Protocol):
    """符号键的调度器视图:对外契约,注册表实现。"""

    def prepare(self, exec: ToolExecutionInput) -> Awaitable[ScheduledToolPreparation]:
        """物化输入、跑有序 pre-execute/guard 门,决定下一阶段。"""
        ...

    def dispatch(self, exec: ToolRunContext) -> Awaitable[ScheduledToolDispatch]:
        """只跑 around-dispatch 与本体阶段。"""
        ...

    def finalize(self, exec: ToolRunContext, result: dict) -> Awaitable[dict]:
        """跑 post-execute 与定义侧内容定稿,再物化并通知。"""
        ...

    def finish(self, exec: ToolRunContext, result: dict) -> dict:
        """跳过 post-execute:只跑内容定稿,再物化并通知。"""
        ...


# ---- 失败词汇 ----


class ToolErrorInfo(TypedDict):
    """一次失败的结构化元数据(与模型面文本并存)。"""

    name: str
    code: str


class ToolFailure(TypedDict):
    """规范失败细节;内部路由信息可选。"""

    message: str
    info: ToolErrorInfo | None


class ToolExecutionSuccess(TypedDict):
    """成功规范执行:执行局部规范值,刻意不进持久事件。"""

    isError: Literal[False]
    value: object
    content: list
    error: None
    meta: object | None
    additionalContexts: list | None
    concludesTurn: Literal[True] | None


class ToolExecutionFailure(TypedDict):
    """失败规范执行:失败永不携带成功值。"""

    isError: Literal[True]
    error: ToolFailure
    value: None
    content: list
    meta: object | None
    additionalContexts: list | None
    concludesTurn: None


#: 一次工具调用的判别式、执行局部结局。
ToolExecutionResult = ToolExecutionSuccess | ToolExecutionFailure


class ToolNotFoundError(HarnessError):
    """模型请求了未注册工具。

    扩展 HarnessError(code: 'UNKNOWN_TOOL'),让未知工具失败和工具
    本体失败一样可路由 —— 重试/沙箱/重放代码能区分两者。
    """

    def __init__(self, tool_name: str, reachable_from: str | None = None) -> None:
        #: 名字可见但被展示面拒绝直调时,告知模型替代路径。
        message = (
            f'unknown tool "{tool_name}"'
            if reachable_from is None
            else f'unknown tool "{tool_name}": {reachable_from}'
        )
        super().__init__(message, "UNKNOWN_TOOL")
        self.name = "ToolNotFoundError"


class ToolOutputError(HarnessError):
    """工具本体或 post-policy 值违反声明的输出契约。"""

    def __init__(self, tool_name: str, violations: list[str]) -> None:
        super().__init__(
            f'tool "{tool_name}" returned invalid output: {"; ".join(violations)}',
            "INVALID_TOOL_OUTPUT",
        )
        self.name = "ToolOutputError"
        self.violations = list(violations)


def error_message(error) -> str:
    """尽力从任意抛值取出人类可读消息。

    Error 实例用 .message;带字符串 message 属性的普通对象
    (如 throw {'message': 'denied'})同样用;其余字符串化。
    错误归一化是最外层安全边界,兜底必须总成功。
    """
    try:
        message = None
        try:
            message = getattr(error, "message", None)
        except Exception:  # noqa: BLE001 -- 敌意属性可让 getattr 本身抛
            message = None
        # 参考实现: 非 Error 对象带字符串 message 属性/键(throw {message: 'denied'})
        if not isinstance(message, str) and isinstance(error, dict):
            value = error.get("message")
            if isinstance(value, str):
                message = value
        if isinstance(message, str):
            return message
        return str(error)
    except Exception:  # noqa: BLE001 -- 字符串化也可能失败,兜底必须总成功
        return "<unprintable thrown value>"


def failure_message_from_content(content: list) -> str:
    """从策略反馈内容块推导失败消息,不改动渲染块。"""
    text = "\n".join(
        block["text"] if block.get("type") == "text" else f"[{block.get('type')} content]"
        for block in content
    )
    return text if text else "tool result blocked by post-execute policy"


def tool_error_result(error, content: list | None = None) -> dict:
    """把一次失败归一成 ToolExecutionResult 的失败面。

    内容块默认 ``{name}: {message}`` 文本(不带 'Error: ' 信封);
    info 只保留 HarnessError 的结构化 name/code,路由按它走。
    """
    message = error_message(error)
    info: ToolErrorInfo | None = None
    if isinstance(error, HarnessError):
        info = {"name": getattr(error, "name", error.__class__.__name__), "code": error.code}
    if content is None:
        name = getattr(error, "name", "Error")
        content = [{"type": "text", "text": f"{name}: {message}"}]
    return {
        "isError": True,
        "error": {"message": message, "info": info},
        "content": content,
    }


# ---- 注册定义契约(注册表批次用) ----


class ToolOutputDefinition(TypedDict):
    """工具侧规范输出契约:本体返回 JSON 值后按它投影。"""

    #: 对每个成功的规范值强制的原始 JSON Schema。
    schema: object
    #: 从已校验参数与值到 Native/模型内容的纯投影。
    render: Callable
    #: 纯可重放的展示投影;只对顶层调用计算。
    presentationMeta: Callable | None


class ToolDefinition(TypedDict):
    """一个注册工具:它的 schema 加执行函数(完整契约见 参考实现)。"""

    name: str
    description: str
    parameters: dict
    output: ToolOutputDefinition
    execute: Callable
    finalizeContent: Callable | None
    timeoutMs: int | None
    isConcurrencySafe: Callable | None
    presentCall: Callable | None
    presentResult: Callable | None


class ToolResult(TypedDict):
    """交给 presentResult 的已完成结局。"""

    content: list
    isError: bool
    meta: object | None


# ---- 策略决策与配置 ----


class PreToolDecision(TypedDict):
    """派发前决策:allow 运行;deny 物化错误;ask 等批准。"""

    kind: Literal["allow", "deny", "ask"]
    reason: str | None


class PostToolDecision(TypedDict):
    """派发后决策:接受/替换投影/附加上下文/转为纠错错误。"""

    kind: Literal["accept", "block"]
    content: list | None
    value: object | None
    feedback: list | None
    additionalContexts: list | None


#: 注册表如何向模型展示工具:native 发全部可见 schema;code 只发
#: run_code + 生成的 SDK 提示;both 两种都发。
ToolPresentationMode = Literal["native", "code", "both"]


class Config(TypedDict):
    """插件配置:模型展示模式与 code 模式的并发上限。"""

    mode: ToolPresentationMode | None
    maxParallelSubCalls: int | None


class ToolRestriction(TypedDict):
    """作用域级对全局工具的过滤:允许/拒绝名单。"""

    allow: list | None
    deny: list | None


# ---- 服务骨架(空注册表 fail-closed 语义) ----


class _EmptyScheduler:
    """空注册表调度器:任何调用都归为未知工具失败。

    注册表批次实现 prepare/dispatch/finalize/finish 后整体替换;
    当前语义是契约成立的行为 —— 无定义 = 每次派发 UNKNOWN_TOOL,
    流程可运行、可测试,而不是崩溃。
    """

    async def prepare(self, exec: ToolExecutionInput) -> ScheduledToolPreparation:
        return {
            "kind": "final-result",
            "exec": exec,  # type: ignore[typeddict-item] -- 契约版未补 token
            "result": tool_error_result(ToolNotFoundError(exec["name"])),
        }

    async def dispatch(self, exec: ToolRunContext) -> ScheduledToolDispatch:
        #: prepare 空表下总是 final-result,dispatch 不可达。
        raise RuntimeError("tools registry is not implemented; dispatch() is unreachable")

    async def finalize(self, exec: ToolRunContext, result: dict) -> dict:
        return result

    def finish(self, exec: ToolRunContext, result: dict) -> dict:
        return result


def _resolve_max_parallel_sub_calls(value: int | None) -> int:
    """code 模式重叠上限:正整数默认 10(与 agent loop 调度器默认一致)。"""
    cap = value if value is not None else 10
    if not isinstance(cap, int) or cap < 1:
        raise TypeError("maxParallelSubCalls must be a positive integer")
    return cap


class ToolRuntime(Service):
    """工具注册表与执行管线的服务面(ctx.tools)。

    批次 2 只落地契约:执行面以空注册表语义运行 —— ``executionMode``
    对任何调用判 exclusive(无 isConcurrencySafe 定义 = fail-closed),
    scheduler 对任何调用给 UNKNOWN_TOOL。注册与执行瀑布在后续批次。
    """

    #: 参考实现经 `static inject = ['systemPrompt']` 声明依赖;Python 侧
    #: 不强制注入,构造时可直接用已挂载的服务。
    def __init__(self, ctx, config: Config | None = None) -> None:
        super().__init__(ctx, "tools")
        config = config or {}
        self.defaultMode: ToolPresentationMode = config.get("mode") or "native"
        self.maxParallelSubCalls = _resolve_max_parallel_sub_calls(config.get("maxParallelSubCalls"))
        #: 调度器槽(符号键):参考实现 是实例上的 symbol 属性,这里经
        #: __getitem__ 提供同形访问。
        self._scheduler: ToolRuntimeScheduler = _EmptyScheduler()

    def __getitem__(self, key) -> ToolRuntimeScheduler:
        if key is TOOL_RUNTIME_SCHEDULER:
            return self._scheduler
        raise KeyError(key)

    def executionMode(self, exec: ToolExecutionInput) -> ToolExecutionMode:
        """判定一次待发调度的模式:只有分类器精确返回 True 才 parallel。

        未知、隐藏、未声明、非法或抛错的分类器一律 exclusive(fail-
        closed)。契约版注册表为空:任何调用都是 exclusive —— 注册表
        批次在此查找可见定义并调用 isConcurrencySafe 后替换。
        """
        return {"kind": "exclusive"}
