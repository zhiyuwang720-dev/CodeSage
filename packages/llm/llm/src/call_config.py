"""调用配置:一次模型调用的完整规格,与传输层解耦。

调用配置回答一个问题:这次调用用什么模型、以什么方式调用。它把
「指针解析结果」与「实际发请求」分开 —— 调用方先拿到一份稳定的
配置,再决定如何执行;提供者注册、消费者改造、中间件观察,都以
这份配置为公共语言,彼此不需要知道对方的内部形状。
"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class LlmCallConfig:
    """一次调用的规格:提供者 + 模型 + 行为参数。

    冻结不可变:配置在派发路径上流转,任何一方都能安全持有,
    不会因为别处顺手改了一笔而悄悄改变这次调用的语义。
    与 TS 版 dsh-llm 的 LlmCallConfig 字段一一对应,命名改 snake_case。
    """

    #: 提供者名(内置 anthropic / openai_compatible,或经接缝注册的外部名)
    provider: str
    #: 模型名(提供者自己的命名)
    model: str
    #: 推理强度档(deepseek 系的心智努力,可空表示提供者默认)
    reasoning_effort: str | None = None
    #: 采样温度,可空表示提供者默认
    temperature: float | None = None
    #: 单次回复上限,可空表示默认上限
    max_tokens: int | None = None
    #: 停止序列,命中即停;可空表示不设
    stop: list[str] | None = None


def call_config_equals(left: LlmCallConfig, right: LlmCallConfig) -> bool:
    """两份配置是否等价:逐字段比较,缺省字段一律视为 None。

    缺省一致性:显式的 None 与未提供的缺省值等价 —— 调用方
    补全与不补全不该造成配置「看起来变了」。
    """
    if left is right:
        return True
    if not isinstance(left, LlmCallConfig) or not isinstance(right, LlmCallConfig):
        return False
    for field in fields(LlmCallConfig):
        if getattr(left, field.name) != getattr(right, field.name):
            return False
    return True
