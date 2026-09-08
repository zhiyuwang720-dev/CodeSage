"""Factory for creating provider adapters."""

from __future__ import annotations

from typing import Any, Dict, List

from .adapters import (
    AnthropicAdapter,
    BaiduAdapter,
    DoubaoAdapter,
    GeminiNativeAdapter,
    LiteLLMAdapter,
    MinimaxAdapter,
    OpenAIResponsesAdapter,
)
from .base_adapter import BaseLLMAdapter
from .protocols.registry import canonical_endpoint_protocol, get_provider_metadata
from .types import DEFAULT_MODELS, LLMConfig, LLMProvider


NATIVE_ONLY_PROVIDERS = {
    LLMProvider.BAIDU,
    LLMProvider.MINIMAX,
    LLMProvider.DOUBAO,
}

OPENAI_CHAT_NATIVE_CAPABLE_PROVIDERS = {
    LLMProvider.MINIMAX,
    LLMProvider.DOUBAO,
}


class LLMFactory:
    """Create and cache LLM adapters."""

    _adapters: Dict[str, BaseLLMAdapter] = {}

    @classmethod
    def create_adapter(cls, config: LLMConfig) -> BaseLLMAdapter:
        cache_key = cls._get_cache_key(config)
        if cache_key in cls._adapters:
            return cls._adapters[cache_key]

        adapter = cls._instantiate_adapter(config)
        cls._adapters[cache_key] = adapter
        return adapter

    @classmethod
    def _instantiate_adapter(cls, config: LLMConfig) -> BaseLLMAdapter:
        if not config.model:
            config.model = cls.get_default_model(config.provider)

        endpoint_protocol = canonical_endpoint_protocol(config.endpoint_protocol)
        config.endpoint_protocol = endpoint_protocol

        if cls._uses_anthropic_native_protocol(config):
            return AnthropicAdapter(config)
        if endpoint_protocol == "openai_responses" and config.provider == LLMProvider.OPENAI:
            return OpenAIResponsesAdapter(config)
        if endpoint_protocol == "gemini_native" and config.provider == LLMProvider.GEMINI:
            return GeminiNativeAdapter(config)

        if (
            config.provider in NATIVE_ONLY_PROVIDERS
            and not (
                endpoint_protocol == "openai_chat"
                and config.provider in OPENAI_CHAT_NATIVE_CAPABLE_PROVIDERS
                and LiteLLMAdapter.supports_provider(config.provider)
            )
        ):
            return cls._create_native_adapter(config)

        if LiteLLMAdapter.supports_provider(config.provider):
            return LiteLLMAdapter(config)

        raise ValueError(f"Unsupported LLM provider: {config.provider}")

    @staticmethod
    def _uses_anthropic_native_protocol(config: LLMConfig) -> bool:
        endpoint_protocol = canonical_endpoint_protocol(config.endpoint_protocol)
        return endpoint_protocol == "anthropic_messages" and config.provider in {
            LLMProvider.CLAUDE,
            LLMProvider.DEEPSEEK,
        }

    @classmethod
    def _create_native_adapter(cls, config: LLMConfig) -> BaseLLMAdapter:
        native_adapter_map = {
            LLMProvider.BAIDU: BaiduAdapter,
            LLMProvider.MINIMAX: MinimaxAdapter,
            LLMProvider.DOUBAO: DoubaoAdapter,
        }
        adapter_class = native_adapter_map.get(config.provider)
        if not adapter_class:
            raise ValueError(f"Unsupported native adapter provider: {config.provider}")
        return adapter_class(config)

    @classmethod
    def _get_cache_key(cls, config: LLMConfig) -> str:
        api_key_prefix = config.api_key[:8] if config.api_key else "no-key"
        return (
            f"{config.provider.value}:{config.model}:{config.base_url or ''}:"
            f"{canonical_endpoint_protocol(config.endpoint_protocol)}:{config.tool_message_format}:{api_key_prefix}:"
            f"{config.timeout}:{config.temperature}:{config.max_tokens}:{config.top_p}:"
            f"{config.frequency_penalty}:{config.presence_penalty}"
        )

    @classmethod
    def clear_cache(cls) -> None:
        cls._adapters.clear()

    @classmethod
    def get_supported_providers(cls) -> List[LLMProvider]:
        return list(LLMProvider)

    @classmethod
    def get_default_model(cls, provider: LLMProvider) -> str:
        return str(get_provider_metadata(provider).get("default_model") or DEFAULT_MODELS.get(provider, "gpt-4o-mini"))

    @classmethod
    def get_available_models(cls, provider: LLMProvider) -> List[str]:
        return list(get_provider_metadata(provider).get("models") or [])

    @classmethod
    def get_provider_metadata(cls, provider: LLMProvider) -> Dict[str, Any]:
        return get_provider_metadata(provider)
