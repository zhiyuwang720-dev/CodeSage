from app.services.llm.factory import LLMFactory
from app.services.llm.adapters.gemini_native_adapter import GeminiNativeAdapter
from app.services.llm.adapters.openai_responses_adapter import OpenAIResponsesAdapter
from app.services.llm.protocols.registry import (
    canonical_endpoint_protocol,
    get_model_capabilities,
    resolve_tool_message_format,
)
from app.services.llm.service import LLMService
from app.services.llm.types import DEFAULT_BASE_URLS, DEFAULT_MODELS, LLMProvider
from app.services.llm.types import LLMConfig


def test_provider_registry_exposes_current_models_and_mimo() -> None:
    providers = {provider.value for provider in LLMFactory.get_supported_providers()}

    assert "mimo" in providers
    assert LLMFactory.get_default_model(LLMProvider.CLAUDE) == "claude-opus-4-8"
    assert "claude-opus-4-8" in LLMFactory.get_available_models(LLMProvider.CLAUDE)
    assert "gpt-5.5" in LLMFactory.get_available_models(LLMProvider.OPENAI)
    assert "gpt-5.6" in LLMFactory.get_available_models(LLMProvider.OPENAI)
    assert "deepseek-v4-pro" in LLMFactory.get_available_models(LLMProvider.DEEPSEEK)
    assert DEFAULT_MODELS[LLMProvider.MIMO] == "mimo-v2.5-pro"
    assert DEFAULT_BASE_URLS[LLMProvider.MIMO] == "https://api.xiaomimimo.com/v1"


def test_provider_metadata_includes_protocol_and_tool_capabilities() -> None:
    openai_metadata = LLMFactory.get_provider_metadata(LLMProvider.OPENAI)
    deepseek_metadata = LLMFactory.get_provider_metadata(LLMProvider.DEEPSEEK)
    gemini_metadata = LLMFactory.get_provider_metadata(LLMProvider.GEMINI)
    mimo_metadata = LLMFactory.get_provider_metadata(LLMProvider.MIMO)

    assert openai_metadata["default_endpoint_protocol"] == "openai_responses"
    assert "openai_chat" in openai_metadata["supported_endpoint_protocols"]
    assert "openai_responses" in openai_metadata["supported_endpoint_protocols"]
    assert "anthropic_messages" in deepseek_metadata["supported_endpoint_protocols"]
    assert gemini_metadata["default_endpoint_protocol"] == "gemini_native"
    assert mimo_metadata["tool_capability"]["reasoning_content"] is True


def test_claude_model_capabilities_describe_opus_4_8_limits() -> None:
    claude_metadata = LLMFactory.get_provider_metadata(LLMProvider.CLAUDE)
    opus_caps = get_model_capabilities(LLMProvider.CLAUDE, "claude-opus-4.8")
    sonnet_caps = get_model_capabilities(LLMProvider.CLAUDE, "claude-sonnet-4-5")

    assert claude_metadata["model_capabilities"]["claude-opus-4-8"]["supports_temperature"] is False
    assert opus_caps["supports_temperature"] is False
    assert opus_caps["supports_assistant_prefill"] is False
    assert sonnet_caps["supports_temperature"] is True
    assert sonnet_caps["supports_assistant_prefill"] is True


def test_protocol_aliases_and_tool_format_resolution() -> None:
    assert canonical_endpoint_protocol("openai_compatible") == "openai_chat"
    assert canonical_endpoint_protocol("anthropic") == "anthropic_messages"
    assert canonical_endpoint_protocol("google") == "gemini_native"

    assert resolve_tool_message_format("openai_responses", provider="openai") == "openai_tools"
    assert resolve_tool_message_format("anthropic_messages", provider="deepseek") == "anthropic_blocks"
    assert resolve_tool_message_format("gemini_native", provider="gemini") == "gemini_parts"


def test_llm_service_parses_mimo_and_agent_protocol_overrides() -> None:
    service = LLMService(
        user_config={
            "llmConfig": {
                "llmProvider": "openai",
                "llmModel": "gpt-5.5",
                "endpointProtocol": "openai_responses",
                "toolMessageFormat": "auto",
                "agentConfigs": {
                    "finding": {
                        "enabled": True,
                        "llmProvider": "mimo",
                        "llmModel": "mimo-v2.5-pro",
                        "endpointProtocol": "openai_chat",
                        "toolMessageFormat": "openai_tools",
                    }
                },
            }
        }
    )

    config = service.get_agent_config("finding")

    assert config.provider is LLMProvider.MIMO
    assert config.model == "mimo-v2.5-pro"
    assert config.endpoint_protocol == "openai_chat"
    assert config.tool_message_format == "openai_tools"


def test_factory_routes_native_protocol_adapters() -> None:
    LLMFactory.clear_cache()

    responses_adapter = LLMFactory.create_adapter(
        LLMConfig(
            provider=LLMProvider.OPENAI,
            api_key="sk-test",
            model="gpt-5.5",
            endpoint_protocol="openai_responses",
        )
    )
    gemini_adapter = LLMFactory.create_adapter(
        LLMConfig(
            provider=LLMProvider.GEMINI,
            api_key="gemini-test",
            model="gemini-3.5-flash",
            endpoint_protocol="gemini_native",
        )
    )

    assert isinstance(responses_adapter, OpenAIResponsesAdapter)
    assert isinstance(gemini_adapter, GeminiNativeAdapter)


def test_factory_cache_distinguishes_sampling_configuration() -> None:
    LLMFactory.clear_cache()
    base = dict(
        provider=LLMProvider.MOONSHOT,
        api_key="moonshot-test",
        model="kimi-k2.6",
        endpoint_protocol="openai_chat",
    )

    automatic = LLMFactory.create_adapter(LLMConfig(**base))
    configured = LLMFactory.create_adapter(LLMConfig(**base, temperature=1, top_p=0.95))

    assert automatic is not configured
