"""llm 服务:能力接缝 —— 契约与消费者同包,提供者独立演进。

一个组合里要跑模型,它该面向什么编程?
答案不是某一家提供者的SDK,而是本包定义的服务契约:请求消息用 ContentBlock(仓库内部消息契约,全系统唯一形状)
调用经 complete/stream 发起,配置经指针解析。消费者只认识这份契约。

提供者怎么进来?
经能力接缝:register_provider 把「提供者名 →适配器工厂」注册到服务上,底层是包内的 LLMClient(重试、回退、成本、流式组装都是它的本职)。
注册挂在服务所在 fiber 的生命周期上 —— 组合卸载,注册自动撤销,不留幽灵适配器。

砍掉的:图像、模型发现、attribution —— 都不在最小 harness 的
核心路径上,需要时作为独立提供者/能力再加回来。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import httpx
from cordis import Context, Service

from .adapters.base import BaseAdapter
from .call_config import LlmCallConfig
from .client import LLMClient, ModelProfile
from .types import LLMRequest, LLMResponse, StreamEvent


class ProviderRegistration:
    """一次注册的句柄:撤销 = 逆序解除全部名字的注册,幂等。"""

    def __init__(self, disposers: list[Callable[[], None]]) -> None:
        self._disposers = disposers

    def dispose(self) -> None:
        """解除注册;重复调用无副作用。"""
        for disposer in reversed(self._disposers):
            disposer()
        self._disposers = []


class LLMService(Service):
    """llm 服务(ctx 键 llm):包装 LLMClient,开能力接缝。

    构造时可传入现成的 LLMClient(测试注入、共享实例),否则按仓库
    默认配置自建。服务自身不管理 http 生命周期 —— client 归调用方
    持有,组合内多处共享同一个 client 是常态。
    """

    provide = "llm"

    def __init__(self, ctx: Context, *, client: LLMClient | None = None) -> None:
        #: 取消事件:服务级的 in-flight 请求中止闸门(仅自建 client 时接线)
        self._cancel_event: asyncio.Event | None = None
        if client is not None:
            self.client = client
        else:
            self._cancel_event = asyncio.Event()
            self.client = LLMClient(cancel_event=self._cancel_event)
        super().__init__(ctx)

    # ---- 能力接缝:提供者注册 ----

    def register_provider(
        self,
        providers: list[str],
        factory: Callable[[ModelProfile, httpx.AsyncClient], BaseAdapter],
    ) -> ProviderRegistration:
        """把一组提供者名注册到同一个适配器工厂,全有或全无。

        任一名字已注册则整体失败并回滚本次已注册的名字 —— 注册是
        原子动作,不存在注册到一半的中间态。返回的句柄由调用方持有,
        组合卸载时也能随 fiber 撤销。
        """
        disposers: list[Callable[[], None]] = []
        try:
            for name in providers:
                disposers.append(self.client.register_provider_factory(name, factory))
        except ValueError:
            for disposer in reversed(disposers):
                disposer()
            raise
        return ProviderRegistration(disposers)

    def list_providers(self) -> list[str]:
        """当前已注册的提供者名(接缝是唯一入口,未注册即不可用)。"""
        return sorted(self.client._provider_factories)  # noqa: SLF001

    # ---- 调用入口 ----

    async def complete(self, request: LLMRequest, *, model: str = "main") -> LLMResponse:
        """发起一次非流式调用;指针/配置解析、重试、成本都在 client 内。"""
        return await self.client.complete(request, model=model)

    def stream(self, request: LLMRequest, *, model: str = "main") -> AsyncIterator[StreamEvent]:
        """发起一次流式调用,消费事件序列。"""
        return self.client.stream(request, model=model)

    def resolve_call_config(self, pointer: str) -> LlmCallConfig:
        """把指针解析成一份稳定的调用配置。

        指针可以是别名(main/task/compact/quick)、配置里的 profile 名、
        或 提供者:模型 字面量 —— 解析规则与 client 完全一致,这里
        把结果投影成调用配置形状。
        """
        profile = self.client.resolve_profile(pointer)
        return LlmCallConfig(provider=profile.provider, model=profile.model)

    def cancel(self) -> None:
        """中止所有 in-flight 请求(仅自建 client 时生效)。

        外部注入的 client 自带取消语义,归注入方管。
        """
        if self._cancel_event is not None:
            self._cancel_event.set()
