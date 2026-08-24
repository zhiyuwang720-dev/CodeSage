"""llm_anthropic 提供者包:Anthropic Messages API 原生适配。

对应 DSH 能力家族的提供者独立演进形态:适配器住在独立包,经能力
接缝注册到 llm 服务。内部消息契约本就是 Anthropic 形状,转换近乎
透传 —— 本包的存在意义与其余提供者包一致:传输逻辑不进契约层,
更换/演进 Anthropic 适配器不触碰服务与消费者。
"""

from __future__ import annotations

import httpx

from llm import ModelProfile
from llm.adapters.base import BaseAdapter

from .anthropic import AnthropicAdapter

__all__ = ["AnthropicAdapter", "install", "register"]

#: 本包注册的提供者名
PROVIDERS = ["anthropic"]


def _make_adapter(profile: ModelProfile, http: httpx.AsyncClient) -> BaseAdapter:
    """适配器工厂:补上缺省密钥环境变量,再交给适配器。"""
    if profile.api_key_env is None:
        profile.api_key_env = "ANTHROPIC_API_KEY"
    return AnthropicAdapter(profile, http)


def register(ctx) -> None:
    """把本包的全部提供者名注册到 ctx.llm 服务的接缝上。"""
    ctx.llm.register_provider(PROVIDERS, _make_adapter)


def install(ctx) -> None:
    """安装本提供者包(register 的别名,组合语义更直观)。"""
    register(ctx)
