"""Integration tests for the LLM layer — DeepSeek adapter via factory."""

import os

import pytest

from app.service.llm.factory import LLMFactory
from app.service.llm.types import (
    LLMConfig,
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> LLMConfig:
    """Build a DeepSeek LLMConfig, reading DEEPSEEK_API_KEY from env."""
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY not set")

    defaults = {
        "provider": LLMProvider.DEEPSEEK,
        "api_key": api_key,
        "model": "deepseek-v4-pro",
        "timeout": 60,
        "max_tokens": 512,
        "temperature": 0.0,
    }
    defaults.update(overrides)
    return LLMConfig(**defaults)


def _user_msg(text: str) -> LLMMessage:
    return LLMMessage(role="user", content=text)


def _system_msg(text: str) -> LLMMessage:
    return LLMMessage(role="system", content=text)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class TestDeepSeekBasic:
    """Basic connectivity and response tests."""

    async def test_simple_completion(self):
        """A single-turn question should return a non-empty response."""
        config = _make_config()
        adapter = LLMFactory.create_adapter(config)

        request = LLMRequest(messages=[_user_msg("用一句话介绍 Python")])
        response = await adapter.complete(request)

        assert isinstance(response, LLMResponse)
        assert response.content, "response content should not be empty"
        assert response.model is not None
        assert response.usage is not None
        assert response.usage.total_tokens > 0

        await adapter.close()

    async def test_system_prompt(self):
        """The adapter should respect a system prompt."""
        config = _make_config()
        adapter = LLMFactory.create_adapter(config)

        request = LLMRequest(
            messages=[
                _system_msg("你是一个只会用 JSON 回答的助手，不要输出任何非 JSON 内容。"),
                _user_msg('输出 {{"hello": "world"}}'),
            ]
        )
        response = await adapter.complete(request)

        assert response.content, "response content should not be empty"
        # With system prompt asking for JSON, response should contain braces
        assert "{" in response.content and "}" in response.content, (
            f"expected JSON-like output, got: {response.content[:200]}"
        )

        await adapter.close()

    async def test_temperature_effect(self):
        """temperature=0 should produce deterministic-ish output."""
        config = _make_config(temperature=0.0)
        adapter = LLMFactory.create_adapter(config)

        request = LLMRequest(messages=[_user_msg("回复'Hello'两个字，不要其他内容")])

        resp1 = await adapter.complete(request)
        resp2 = await adapter.complete(request)

        # With temperature 0, responses should be identical or nearly so
        assert resp1.content.strip() == resp2.content.strip(), (
            f"temperature=0 should yield identical outputs, "
            f"got:\n  {resp1.content!r}\n  {resp2.content!r}"
        )

        await adapter.close()

    async def test_multi_turn_conversation(self):
        """Multi-turn conversation should maintain context."""
        config = _make_config()
        adapter = LLMFactory.create_adapter(config)

        messages = [
            _user_msg("我叫小明，今年18岁。请记住。"),
            LLMMessage(role="assistant", content="好的小明，我记住了，你今年18岁。"),
            _user_msg("我叫什么名字？多大年纪？"),
        ]
        request = LLMRequest(messages=messages)
        response = await adapter.complete(request)

        assert "小明" in response.content, (
            f"multi-turn context lost, got: {response.content[:200]}"
        )

        await adapter.close()


class TestDeepSeekUsage:
    """Token usage reporting tests."""

    async def test_usage_fields_present(self):
        """Response should include token usage counts."""
        config = _make_config()
        adapter = LLMFactory.create_adapter(config)

        request = LLMRequest(messages=[_user_msg("hello")])
        response = await adapter.complete(request)

        assert response.usage is not None
        assert response.usage.prompt_tokens > 0
        assert response.usage.completion_tokens > 0
        assert response.usage.total_tokens == (
            response.usage.prompt_tokens + response.usage.completion_tokens
        )

        await adapter.close()


class TestFactory:
    """Factory correctness tests."""

    def test_create_deepseek_adapter(self):
        """Factory should return a DeepSeekAdapter for DEEPSEEK provider."""
        config = LLMConfig(
            provider=LLMProvider.DEEPSEEK,
            api_key="sk-test",
            model="deepseek-chat",
        )
        adapter = LLMFactory.create_adapter(config)
        assert adapter.get_provider() == LLMProvider.DEEPSEEK
        assert adapter.get_model() == "deepseek-chat"

    def test_factory_caches_same_config(self):
        """Same config should return the same adapter instance."""
        LLMFactory.clear_cache()
        config = LLMConfig(
            provider=LLMProvider.DEEPSEEK,
            api_key="sk-test",
            model="deepseek-chat",
        )
        a1 = LLMFactory.create_adapter(config)
        a2 = LLMFactory.create_adapter(config)
        assert a1 is a2

    def test_unsupported_provider_raises(self):
        """Unsupported provider should raise ValueError with clear message."""
        with pytest.raises(ValueError, match="Unsupported LLM provider"):
            LLMFactory.create_adapter(
                LLMConfig(provider=LLMProvider.CLAUDE, api_key="x", model="claude-4")
            )

    def test_clear_cache(self):
        """clear_cache should evict all cached adapters."""
        config = LLMConfig(
            provider=LLMProvider.DEEPSEEK,
            api_key="sk-test",
            model="deepseek-chat",
        )
        a1 = LLMFactory.create_adapter(config)
        LLMFactory.clear_cache()
        a2 = LLMFactory.create_adapter(config)
        assert a1 is not a2


class TestErrorHandling:
    """Error handling tests."""

    async def test_invalid_api_key(self):
        """An invalid API key should raise an error."""
        config = LLMConfig(
            provider=LLMProvider.DEEPSEEK,
            api_key="sk-invalid-key-12345",
            model="deepseek-chat",
            timeout=10,
        )
        adapter = LLMFactory.create_adapter(config)

        with pytest.raises(Exception) as exc_info:
            await adapter.complete(
                LLMRequest(messages=[_user_msg("hi")])
            )
        # Should be an LLMError with status_code 401 (auth failure)
        from app.service.llm.types import LLMError
        assert isinstance(exc_info.value, LLMError), (
            f"expected LLMError, got {type(exc_info.value).__name__}: {exc_info.value}"
        )
        assert exc_info.value.status_code == 401, (
            f"expected 401, got {exc_info.value.status_code}: {exc_info.value}"
        )

        await adapter.close()
