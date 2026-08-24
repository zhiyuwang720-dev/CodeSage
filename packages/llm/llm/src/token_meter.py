"""token 计量:固定密度启发式估算 + 真实 usage 聚合。

为什么要估算:真实 token 数只在模型返回后才知道,而「发之前就想
知道要花多少」的场景(上下文压力、预算护栏)等不到那一刻。这里
用固定密度近似 —— 每 4 个字符折 1 token,每个消息块加 4 的固定
开销,每条消息加 4 的角色开销 —— 与 TS 版 dsh-token-meter 的
estimate 模块同源。它不是计费依据,是量级参考。

真实 usage 由提供者随响应带回(Usage 结构),这里按 提供者+模型
聚合成桶,累积出会话口径的 token 账本。

DSH 原版的 replay 感知投影(依赖会话事件溯源,逐事件重放定价)
不在本包范围内 —— 那是 session 包的能力接缝铺好之后的事,
此处只保留独立的纯估算与聚合。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from cordis import Context, Service

from .types import ContentBlock, LLMRequest, Message, Usage

#: 每 token 的字符数密度(固定启发式,DSH 同值)
CHARS_PER_TOKEN = 4
#: 每个消息块的固定开销(token)
BLOCK_OVERHEAD = 4
#: 每条消息的角色开销(token)
ROLE_OVERHEAD = 4


def estimate_text(text: str) -> int:
    """一段纯文本的估算:按字符密度折算,再加块开销。"""
    return math.ceil(len(text) / CHARS_PER_TOKEN) + BLOCK_OVERHEAD


def estimate_content(blocks: list[ContentBlock] | None) -> int:
    """一串消息块的估算:按块类型递归计价。

    - 文本/思考块:按文本密度;
    - 工具调用块:工具名 + 参数 JSON 分别计价;
    - 工具结果块:内容递归计价,再加块开销(它本身是个嵌套载体);
    - 未知形状:按 JSON 序列化后的文本计价,保证任何块都有价可估。
    """
    total = 0
    for block in blocks or []:
        if block.type in ("text", "thinking"):
            total += estimate_text(block.text or "")
        elif block.type == "tool_use":
            total += estimate_text(block.name or "")
            total += estimate_text(json.dumps(block.input, ensure_ascii=False))
        elif block.type == "tool_result":
            if isinstance(block.content, str):
                total += estimate_text(block.content)
            else:
                total += estimate_content(block.content)
            total += BLOCK_OVERHEAD
        else:
            total += estimate_text(json.dumps(block.__dict__, ensure_ascii=False))
    return total


def estimate_message(message: Message) -> int:
    """一条消息的估算:内容 + 角色开销。"""
    if isinstance(message.content, str):
        return estimate_text(message.content) + ROLE_OVERHEAD
    return estimate_content(message.content) + ROLE_OVERHEAD


def estimate_system_tokens(system: str | list[ContentBlock] | None) -> int:
    """系统提示的估算:文本密度 + 角色开销(与消息同构)。"""
    if system is None:
        return 0
    if isinstance(system, str):
        return math.ceil(len(system) / CHARS_PER_TOKEN) + ROLE_OVERHEAD
    return estimate_content(system) + ROLE_OVERHEAD


def estimate_request(request: LLMRequest) -> int:
    """一次完整请求的估算:系统提示 + 全部消息。"""
    return estimate_system_tokens(request.system) + sum(
        estimate_message(m) for m in request.messages
    )


def usage_tokens(usage: Usage) -> int:
    """一次 usage 的 token 总数:四类输入输出相加,不重复计。

    与 DSH 的 usageTokens 同构:输入 + 缓存读 + 缓存写 + 输出。
    """
    return (
        usage.input_tokens
        + usage.cache_read_tokens
        + usage.cache_write_tokens
        + usage.output_tokens
    )


@dataclass
class UsageBucket:
    """一个 提供者+模型 维度上的 usage 聚合桶,累积真实计费数据。"""

    provider: str
    model: str
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """桶内全部 token:四类输入输出相加(与 usage_tokens 同口径)。"""
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )

    def merge(self, usage: Usage) -> "UsageBucket":
        """把一次 usage 并入桶内,返回自身便于链式调用。"""
        self.input_tokens += usage.input_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        self.output_tokens += usage.output_tokens
        return self


class TokenMeter(Service):
    """token 计量服务:聚合真实 usage,提供请求估算。

    挂在 ctx 键 token-meter 上。桶按 提供者+模型 分账 —— 一个
    组合里可能同时用多家提供者,账要分开记,合起来看才是总量。
    """

    provide = "token-meter"

    def __init__(self, ctx: Context) -> None:
        #: 按 (provider, model) 聚合的桶
        self._buckets: dict[tuple[str, str], UsageBucket] = {}
        super().__init__(ctx)

    def record(self, usage: Usage, *, provider: str, model: str) -> UsageBucket:
        """记一笔真实 usage,返回它落入的桶。"""
        key = (provider, model)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = UsageBucket(provider=provider, model=model)
            self._buckets[key] = bucket
        return bucket.merge(usage)

    def buckets(self) -> list[UsageBucket]:
        """全部桶的只读快照(插入序)。"""
        return list(self._buckets.values())

    def total_tokens(self) -> int:
        """所有桶的 token 总数。"""
        return sum(b.total_tokens for b in self._buckets.values())
