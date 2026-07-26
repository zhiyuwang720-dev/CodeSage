"""Factory for creating provider adapters."""

from __future__ import annotations

from typing import Dict, List

from .adapters import DeepSeekAdapter, OpenAIResponsesAdapter
from .base_adapter import BaseLLMAdapter
from .types import DEFAULT_MODELS, LLMConfig, LLMProvider


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

        if config.provider == LLMProvider.DEEPSEEK:
            return DeepSeekAdapter(config)
        if config.provider == LLMProvider.OPENAI:
            return OpenAIResponsesAdapter(config)

        raise ValueError(
            f"Unsupported LLM provider: {config.provider}. "
            f"Currently supported: {cls.get_supported_providers()}"
        )

    @classmethod
    def _get_cache_key(cls, config: LLMConfig) -> str:
        api_key_prefix = config.api_key[:8] if config.api_key else "no-key"
        return (
            f"{config.provider.value}:{config.model}:{config.base_url or ''}:"
            f"{config.timeout}:{config.temperature}:{config.max_tokens}:{config.top_p}"
        )

    @classmethod
    def clear_cache(cls) -> None:
        cls._adapters.clear()

    @classmethod
    def get_supported_providers(cls) -> List[LLMProvider]:
        return [LLMProvider.DEEPSEEK, LLMProvider.OPENAI]

    @classmethod
    def get_default_model(cls, provider: LLMProvider) -> str:
        return DEFAULT_MODELS.get(provider, "gpt-4o-mini")
