"""llm_deepseek 提供者包:OpenAI 兼容协议适配(DeepSeek / Qwen / GLM / OpenAI)。

对应 DSH 的 dsh-llm-deepseek:提供者在独立包中独立演进,不触碰llm 包与服务消费者。本包只做两件事 ——
持有适配器实现,以及经install 把它注册到 llm 服务的能力接缝上。

注册名:deepseek(DSH 风格名)与 openai_compatible(legacy 配置名,向后兼容)等一套兼容协议名,配置与字面量写哪个都解析到同一适配器。
密钥缺省按提供者取环境变量;profile 显式指定 api_key_env时以它为准。
"""

from __future__ import annotations

import httpx

from llm import LLMRequest, LLMResponse, ModelProfile, StreamEvent
from llm.src.adapters.base import BaseAdapter

from .openai_compatible import OpenAICompatibleAdapter

__all__ = ["OpenAICompatibleAdapter", "PROVIDERS", "install", "register"]

#: 提供者名 → 缺省密钥环境变量(未显式配置时的兜底)
_ENV_BY_PROVIDER = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai_compatible": "OPENAI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "glm": "ZHIPUAI_API_KEY",
}

#: 本包注册的提供者名(一套兼容协议端点共用一个适配器)
PROVIDERS = ["deepseek", "openai_compatible", "openai", "qwen", "glm"]


def _make_adapter(profile: ModelProfile, http: httpx.AsyncClient) -> BaseAdapter:
    """适配器工厂:补上缺省密钥环境变量,再交给适配器。

    工厂是能力接缝的握手点 —— client 只传 profile 与共享 http,
    提供者自己的缺省(密钥环境变量名)在这里补齐,不污染契约层。
    """
    if profile.api_key_env is None:
        profile.api_key_env = _ENV_BY_PROVIDER.get(profile.provider, "OPENAI_API_KEY")
    return OpenAICompatibleAdapter(profile, http)


def register(ctx) -> None:
    """把本包的全部提供者名注册到 ctx.llm 服务的接缝上。"""
    ctx.llm.register_provider(PROVIDERS, _make_adapter)


def install(ctx, config: dict | None = None) -> None:
    """安装本提供者包(register 的别名,组合语义更直观)。

    组合里 load 本包即注册:ctx 上先有 llm 服务,再挂提供者。
    """
    register(ctx)
install.inject = ["llm"]
