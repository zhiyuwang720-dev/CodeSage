"""harness 错误基类与错误链渲染:统一错误事实的携带与展示。

harness 里错误不是「抛出去就完了」:工具结果要保留失败类别、
重放要保留失败事实、UI 要给用户可读的因果链。本模块提供两个面:

- ``HarnessError``:带稳定机器可路由 ``code`` 的错误基类 —— 路由
  永远按 code 走,绝不解析 message 文本(文本是给人看的,会变);
- ``error_chain``:把任意被捕获值渲染成完整因果链文本 —— 只给
  诊断面(日志、通知、错误消息)用,绝不能当结构化数据解析。

错误码常量:``CONTEXT_WINDOW_EXCEEDED`` / ``QUOTA`` /
``EMPTY_RESPONSE`` / ``INVALID_CREDENTIAL`` 是提供者中立的标准码,
适配器把提供者自己的错误归一到这些码上,循环的错误恢复策略
(重试/降级)只认标准码。
"""

from __future__ import annotations

import re

__all__ = [
    "CONTEXT_WINDOW_EXCEEDED_CODE",
    "EMPTY_RESPONSE_CODE",
    "HarnessError",
    "INVALID_CREDENTIAL_CODE",
    "QUOTA_EXCEEDED_CODE",
    "error_chain",
    "is_context_window_exceeded_error",
    "is_harness_error",
    "is_quota_exceeded_error",
]

#: 标准码:请求超过模型上下文窗口
CONTEXT_WINDOW_EXCEEDED_CODE = "CONTEXT_WINDOW_EXCEEDED"
#: 标准码:账户配额/余额耗尽
QUOTA_EXCEEDED_CODE = "QUOTA"
#: 标准码:响应正常完成但没有任何内容块(提供者偶发的退化完成)。
#: 空消息会静默结束回合、用户和循环都无事可做,所以适配器把它
#: 归类为失败而不是产出空 assistant 消息;产物没有耐久记录,
#: 重试策略视为可安全重试。
EMPTY_RESPONSE_CODE = "EMPTY_RESPONSE"
#: 标准码:凭据存在但不可用(畸形而非缺失)。与 MISSING_CREDENTIAL
#: 区分,因为修法不同:改存量值而不是补一个;刻意排除在默认可重试
#: 集外 —— 畸形凭据每次尝试都以同样方式失败。
INVALID_CREDENTIAL_CODE = "INVALID_CREDENTIAL"


class HarnessError(Exception):
    """harness 错误的基类:稳定 code + 链式 cause。

    ``code`` 是稳定、程序化的失败类别(如 ``NO_ADAPTER`` /
    ``INVALID_ARGS`` / ``INVARIANT``),与人类可读的 ``message``
    分开;子类覆盖 ``name`` 为自身类名(对齐 Error.name 语义)。
    """

    #: 稳定机器可路由的失败类别;路由按它走,绝不解析 message
    code: str

    def __init__(self, message: str, code: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.name = self.__class__.__name__
        if cause is not None:
            self.__cause__ = cause


def is_harness_error(value: object) -> bool:
    """被捕获值是否真是 HarnessError 实例(运行时边界用 isinstance)。"""
    return isinstance(value, HarnessError)


#: 结构化短语:点名「上下文长度/窗口被超」的提供者措辞
_STRUCTURED_CONTEXT_OVERFLOW = re.compile(
    r"(?:^|[^a-z0-9])context[\s_-](?:length|window)[\s_-]"
    r"(?:exceed(?:ed|s)?|overflow(?:ed)?|limit[\s_-]exceeded)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
#: 把「太大」直接绑到模型上下文容量的措辞
_TOO_LARGE_FOR_CONTEXT = re.compile(
    r"\b(?:request|prompt|input|messages?)\s+(?:is\s+|are\s+)?"
    r"too\s+(?:large|long)\s+for\s+(?:(?:this|the)\s+)?"
    r"(?:model(?:'s)?\s+)?context(?:\s+window)?\b",
    re.IGNORECASE,
)
#: 只有显式以模型上下文为对象的 "exceeds" 措辞才安全
_EXCEEDS_MODEL_CONTEXT = re.compile(
    r"\b(?:input|prompt|request|messages?)\b.{0,40}"
    r"\b(?:exceed(?:s|ed)?|overflows?|is\s+larger\s+than)\b.{0,40}"
    r"\b(?:the\s+)?(?:model(?:'s)?\s+)?context(?:\s+(?:length|window))?\b",
    re.IGNORECASE,
)


def is_context_window_exceeded_error(detail: str) -> bool:
    """识别 OpenAI 兼容提供者与库适配器的上下文溢出措辞。

    适配器把提供者的 code/type/message 文本拼成一个串传入,
    抛式与带内投递两种风格共用同一个分类器。
    """
    return bool(
        _STRUCTURED_CONTEXT_OVERFLOW.search(detail)
        or re.search(r"\b(?:maximum|max)(?:\s+(?:allowed|supported))?\s+context\s+(?:length|window)\b", detail, re.IGNORECASE)
        or _TOO_LARGE_FOR_CONTEXT.search(detail)
        or re.search(r"\b(?:input|prompt|request)\s+(?:is\s+)?too\s+(?:long|large)\s+for\s+(?:this|the)\s+model\b", detail, re.IGNORECASE)
        or _EXCEEDS_MODEL_CONTEXT.search(detail)
    )


def is_quota_exceeded_error(detail: str) -> bool:
    """识别账户配额耗尽措辞:只认终局性的 quota/balance/credit/budget。"""
    return bool(
        re.search(r"\binsufficient[\s_-]+(?:quota|balance|credits?)\b", detail, re.IGNORECASE)
        or re.search(r"\b(?:quota|usage[\s_-]+limit)[\s_-]+(?:exceeded|exhausted|reached)\b", detail, re.IGNORECASE)
        or re.search(r"\bexceed(?:ed|s)?[\s_-]+(?:(?:your|the)[\s_-]+)?(?:current[\s_-]+)?quota\b", detail, re.IGNORECASE)
        or re.search(r"\b(?:balance|credits?)[\s_-]+(?:exhausted|depleted)\b", detail, re.IGNORECASE)
        or re.search(r"\bout[\s_-]+of[\s_-]+(?:credits?|budget)\b", detail, re.IGNORECASE)
    )


def error_chain(value: object) -> str:
    """把被捕获值渲染成完整因果链文本。

    传输包装(如 fetch failed)常掩盖底层失败,这里把 message 与
    全部 cause 链拼出来;循环 cause 用标记截断(AggregateError
    成员用 ``; `` 连接)。只供诊断面渲染 —— 绝不解析结果,
    路由永远走 HarnessError.code。
    """

    #: 追踪活动递归路径(退出时删除):只有真循环被标记,
    #: 菱形共享的 cause 仍完整渲染
    path: set[int] = set()

    def render(current: object) -> str:
        marker = id(current)
        if marker in path:
            return "<circular cause>"
        path.add(marker)
        try:
            if not isinstance(current, BaseException):
                if isinstance(current, dict) and isinstance(current.get("message"), str):
                    return current["message"]
                return str(current)
            # TS 的 Error.message 是标准属性;Python 的 Exception 没有
            # message 属性,str(exc) 就是 args 的渲染(单参数即消息本身)
            message = str(current) or type(current).__name__
            members = ""
            if isinstance(current, BaseExceptionGroup) and len(current.exceptions) > 0:
                members = f" [{'; '.join(render(e) for e in current.exceptions)}]"
            cause_text = ""
            if current.__cause__ is not None:
                cause_text = render(current.__cause__)
            # 包装器(如 HarnessError(String(value), code, cause=value))
            # 逐字重复其 cause;再渲染一遍只是噪音
            cause = "" if cause_text in ("", message) else f": {cause_text}"
            return f"{message}{members}{cause}"
        except Exception:  # noqa: BLE001 -- 渲染器喂给 UI 通知与日志,任何异常都不得逃逸
            return "<unrenderable value>"
        finally:
            path.discard(marker)

    return render(value)
