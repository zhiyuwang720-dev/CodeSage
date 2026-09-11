"""模型目录与配置解析单元测试（P01/P02/P10）。

替代已删除的 factory/protocols 单测：只覆盖当前唯一配置层
`app.execution_plane.models.config` 的语义，危险反例（不可用协议、缺 base_url）
保留。
"""

from __future__ import annotations

import pytest

from app.execution_plane.models.config import (
    MODEL_BOUNDARY_VERSION,
    ModelCatalog,
    ModelConfigurationError,
    canonical_endpoint_protocol,
    canonical_tool_message_format,
    get_model_capabilities,
    parse_provider,
    resolve_sdk_model,
    resolve_tool_message_format,
)
from app.execution_plane.models.types import LLMProvider


def test_catalog_exposes_every_provider() -> None:
    providers = {provider.value for provider in ModelCatalog.supported_providers()}
    assert {"openai", "claude", "deepseek", "qwen", "gemini", "ollama"} <= providers


def test_catalog_defaults_and_model_lists() -> None:
    assert ModelCatalog.default_model(LLMProvider.CLAUDE) == "claude-opus-4-8"
    assert "claude-opus-4-8" in ModelCatalog.available_models(LLMProvider.CLAUDE)
    assert "gpt-5.5" in ModelCatalog.available_models(LLMProvider.OPENAI)
    assert "deepseek-v4-pro" in ModelCatalog.available_models(LLMProvider.DEEPSEEK)


def test_catalog_only_advertises_supported_endpoint_protocols() -> None:
    unsupported = {"openai_responses", "responses", "native"}
    for provider in ModelCatalog.supported_providers():
        metadata = ModelCatalog.provider_metadata(provider)
        assert not (unsupported & set(metadata["supported_endpoint_protocols"])), provider.value
        assert metadata["default_endpoint_protocol"] not in unsupported, provider.value


def test_catalog_marks_baidu_as_unavailable() -> None:
    metadata = ModelCatalog.provider_metadata(LLMProvider.BAIDU)
    assert metadata["default_endpoint_protocol"] is None
    assert metadata["supported_endpoint_protocols"] == []
    assert "unavailable" in str(metadata.get("notes", "")).lower() or metadata["supported_endpoint_protocols"] == []


def test_provider_metadata_carries_tool_capability() -> None:
    deepseek = ModelCatalog.provider_metadata(LLMProvider.DEEPSEEK)
    assert deepseek["tool_capability"]["tools"] is True
    assert deepseek["tool_capability"]["reasoning_content"] is True


def test_model_capabilities_override_per_model() -> None:
    opus = get_model_capabilities(LLMProvider.CLAUDE, "claude-opus-4.8")
    sonnet = get_model_capabilities(LLMProvider.CLAUDE, "claude-sonnet-4-5")
    assert opus["supports_temperature"] is False
    assert sonnet["supports_temperature"] is True


def test_provider_aliases_resolve() -> None:
    assert parse_provider("anthropic") is LLMProvider.CLAUDE
    assert parse_provider("xiaomimimo") is LLMProvider.MIMO
    assert parse_provider("unknown-vendor") is LLMProvider.OPENAI


def test_endpoint_protocol_migration_table() -> None:
    assert canonical_endpoint_protocol("openai_compatible") == "openai_chat"
    assert canonical_endpoint_protocol("anthropic") == "anthropic_messages"
    assert canonical_endpoint_protocol("google") == "gemini_native"
    assert canonical_endpoint_protocol(None) == "openai_chat"


@pytest.mark.parametrize("value", ["native", "openai_responses", "responses", "unknown-protocol"])
def test_unavailable_or_unknown_protocols_fail_explicitly(value: str) -> None:
    with pytest.raises(ModelConfigurationError):
        canonical_endpoint_protocol(value)


def test_tool_message_format_resolution() -> None:
    assert canonical_tool_message_format("follow_protocol") == "auto"
    assert resolve_tool_message_format("anthropic_messages", provider="deepseek") == "anthropic_blocks"
    assert resolve_tool_message_format("gemini_native", provider="gemini") == "gemini_parts"
    assert resolve_tool_message_format("openai_compatible", provider="claude") == "openai_tools"
    assert resolve_tool_message_format(None, requested="legacy") == "legacy_text"
    # 不可用协议即使显式指定 tool 格式也会在解析期报错，避免静默退化
    with pytest.raises(ModelConfigurationError):
        resolve_tool_message_format("openai_responses", provider="openai")


def test_sdk_model_mapping_keeps_endpoints_and_models() -> None:
    assert resolve_sdk_model(LLMProvider.DEEPSEEK, "deepseek-chat", "https://api.deepseek.com") == (
        "openai/deepseek-chat",
        "openai_compatible",
    )
    assert resolve_sdk_model(LLMProvider.CLAUDE, "claude-opus-4-8", None) == (
        "anthropic/claude-opus-4-8",
        "anthropic_messages",
    )
    assert resolve_sdk_model(LLMProvider.OPENAI, "gpt-5.5", None) == ("openai/gpt-5.5", "openai_compatible")
    # 自定义网关保留完整模型名
    assert resolve_sdk_model(LLMProvider.QWEN, "Qwen/Qwen3-8B", "https://api.siliconflow.cn/v1") == (
        "openai/Qwen/Qwen3-8B",
        "openai_compatible",
    )
    # 已带已知前缀的模型名原样保留
    assert resolve_sdk_model(LLMProvider.CLAUDE, "anthropic/claude-opus-4-8", None) == (
        "anthropic/claude-opus-4-8",
        "anthropic_messages",
    )


def test_sdk_model_mapping_rejects_guessing() -> None:
    with pytest.raises(ModelConfigurationError):
        resolve_sdk_model(LLMProvider.QWEN, "some-vendor/model", None)
    with pytest.raises(ModelConfigurationError):
        resolve_sdk_model(LLMProvider.MOONSHOT, "kimi-k2.6", None)
    with pytest.raises(ModelConfigurationError):
        resolve_sdk_model(LLMProvider.OPENAI, "   ", None)


def test_model_boundary_version_is_stable() -> None:
    assert MODEL_BOUNDARY_VERSION == "litellm_sdk_v1"
