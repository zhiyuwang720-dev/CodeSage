from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.llm.types import LLMProvider


PROTOCOL_ALIASES = {
    "openai": "openai_chat",
    "openai_chat": "openai_chat",
    "openai_compatible": "openai_chat",
    "openai-compatible": "openai_chat",
    "chat_completions": "openai_chat",
    "chat_completion": "openai_chat",
    "openai_responses": "openai_responses",
    "responses": "openai_responses",
    "anthropic": "anthropic_messages",
    "anthropic_messages": "anthropic_messages",
    "claude": "anthropic_messages",
    "google": "gemini_native",
    "gemini": "gemini_native",
    "gemini_native": "gemini_native",
}

TOOL_FORMAT_ALIASES = {
    "auto": "auto",
    "follow_protocol": "auto",
    "openai": "openai_tools",
    "openai_tools": "openai_tools",
    "anthropic": "anthropic_blocks",
    "anthropic_blocks": "anthropic_blocks",
    "gemini": "gemini_parts",
    "gemini_parts": "gemini_parts",
    "responses": "responses_items",
    "responses_items": "responses_items",
    "legacy": "legacy_text",
    "legacy_text": "legacy_text",
    "xml": "legacy_text",
    "json": "legacy_text",
}

DEFAULT_MODEL_CAPABILITIES = {
    "supports_temperature": True,
    "supports_assistant_prefill": True,
    "preferred_finalization_mode": "assistant_prefill",
}

PROVIDER_REGISTRY: dict[LLMProvider, dict[str, Any]] = {
    LLMProvider.OPENAI: {
        "label": "OPENAI",
        "default_model": "gpt-5.5",
        "models": ["gpt-5.6", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "o4-mini", "o3"],
        "default_endpoint_protocol": "openai_responses",
        "supported_endpoint_protocols": ["openai_responses", "openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none", "required", "forced"], "parallel_tool_calls": True},
        "notes": "OpenAI official models should prefer the Responses API; Chat Completions remains supported for compatibility.",
    },
    LLMProvider.CLAUDE: {
        "label": "CLAUDE",
        "default_model": "claude-opus-4-8",
        "models": ["claude-opus-4-8", "claude-fable-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-sonnet-4-5"],
        "default_endpoint_protocol": "anthropic_messages",
        "supported_endpoint_protocols": ["anthropic_messages", "openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none", "any", "forced"], "parallel_tool_calls": True},
        "default_model_capabilities": {
            "supports_temperature": True,
            "supports_assistant_prefill": True,
            "preferred_finalization_mode": "assistant_prefill",
        },
        "model_capabilities": {
            "claude-opus-4-8": {
                "supports_temperature": False,
                "supports_assistant_prefill": False,
                "preferred_finalization_mode": "tool_call",
            },
            "claude-opus-4-7": {
                "supports_temperature": False,
                "supports_assistant_prefill": False,
                "preferred_finalization_mode": "tool_call",
            },
            "claude-opus-4-6": {
                "supports_temperature": False,
                "supports_assistant_prefill": False,
                "preferred_finalization_mode": "tool_call",
            },
            "claude-sonnet-4-5": {
                "supports_temperature": True,
                "supports_assistant_prefill": True,
                "preferred_finalization_mode": "assistant_prefill",
            },
        },
    },
    LLMProvider.GEMINI: {
        "label": "GEMINI",
        "default_model": "gemini-3.5-flash",
        "models": [
            "gemini-3.5-flash",
            "gemini-3.1-pro",
            "gemini-3.1-flash-lite",
            "gemini-3-flash",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ],
        "default_endpoint_protocol": "gemini_native",
        "supported_endpoint_protocols": ["gemini_native", "openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none", "any", "validated"], "parallel_tool_calls": True},
    },
    LLMProvider.QWEN: {
        "label": "QWEN",
        "default_model": "qwen3.7-max",
        "models": ["qwen3.7-max", "qwen3.7-plus", "qwen3.6-max", "qwen3.6-plus", "qwen3.6-flash", "qwen3-coder-plus", "qwen3-max-instruct"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none"], "parallel_tool_calls": False},
        "notes": "Qwen/Bailian tool_choice support varies by model and deployment; forced tools should stay opt-in.",
    },
    LLMProvider.DEEPSEEK: {
        "label": "DEEPSEEK",
        "default_model": "deepseek-v4-pro",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat", "anthropic_messages"],
        "tool_capability": {
            "tools": True,
            "tool_choice": ["auto", "none", "required", "forced"],
            "parallel_tool_calls": True,
            "reasoning_content": True,
            "deprecated_aliases": {"deepseek-chat": "2026-07-24T15:59:00Z", "deepseek-reasoner": "2026-07-24T15:59:00Z"},
        },
    },
    LLMProvider.ZHIPU: {
        "label": "ZHIPU",
        "default_model": "glm-5.2",
        "models": ["glm-5.2", "glm-5.1", "glm-5-turbo", "glm-5", "glm-4.7", "glm-4.6", "glm-4.5-air", "glm-4.5-flash"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto"], "parallel_tool_calls": False, "extra_body": {"tool_stream": True}},
    },
    LLMProvider.MOONSHOT: {
        "label": "MOONSHOT",
        "default_model": "kimi-k2.6",
        "models": ["kimi-k2.7-code-highspeed", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking", "kimi-k2"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none"], "parallel_tool_calls": False, "reasoning_content": True},
    },
    LLMProvider.BAIDU: {
        "label": "BAIDU",
        "default_model": "ernie-4.5",
        "models": ["ernie-4.5", "ernie-4.5-21b-a3b-thinking", "ernie-4.0-8k", "ernie-3.5-8k"],
        "default_endpoint_protocol": "native",
        "supported_endpoint_protocols": ["native", "openai_chat"],
        "tool_capability": {"tools": False, "tool_choice": []},
    },
    LLMProvider.MINIMAX: {
        "label": "MINIMAX",
        "default_model": "minimax-m2.7",
        "models": ["minimax-m3", "minimax-m2.7", "minimax-m2.5", "minimax-m2.1", "minimax-m2"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat", "native"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none"], "parallel_tool_calls": False},
    },
    LLMProvider.DOUBAO: {
        "label": "DOUBAO",
        "default_model": "doubao-1.6-pro",
        "models": ["doubao-1.6-pro", "doubao-1.5-pro", "doubao-seed-code", "doubao-seed-1.6"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat", "native"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none"], "parallel_tool_calls": False},
    },
    LLMProvider.MIMO: {
        "label": "MIMO",
        "default_model": "mimo-v2.5-pro",
        "models": ["mimo-v2.5-pro", "mimo-v2.5-flash"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none"], "parallel_tool_calls": False, "reasoning_content": True},
        "notes": "MiMo is treated as an OpenAI Chat compatible reasoning provider; keep tool_choice conservative until endpoint smoke tests confirm forced tool support.",
    },
    LLMProvider.OLLAMA: {
        "label": "OLLAMA",
        "default_model": "llama3.3-70b",
        "models": ["llama3.3-70b", "qwen3-8b", "gemma3-27b", "deepseek-r1", "gpt-oss-120b", "llama3.1-405b", "mistral-nemo", "phi-3"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none"], "parallel_tool_calls": False},
    },
}


def normalize_model_id(model: str | None) -> str:
    return str(model or "").strip().lower().replace(".", "-")


def canonical_endpoint_protocol(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    return PROTOCOL_ALIASES.get(raw, raw or "openai_chat")


def canonical_tool_message_format(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    return TOOL_FORMAT_ALIASES.get(raw, raw or "auto")


def resolve_tool_message_format(endpoint_protocol: str | None, *, provider: str | None = None, requested: str | None = "auto") -> str:
    explicit = canonical_tool_message_format(requested)
    if explicit != "auto":
        return explicit

    protocol = canonical_endpoint_protocol(endpoint_protocol)
    if protocol == "anthropic_messages":
        return "anthropic_blocks"
    if protocol == "gemini_native":
        return "gemini_parts"
    if protocol in {"openai_chat", "openai_responses"}:
        return "openai_tools"
    if str(provider or "").strip().lower() in {"claude", "anthropic"}:
        return "anthropic_blocks"
    return "openai_tools"


def get_model_capabilities(provider: LLMProvider, model: str | None) -> dict[str, Any]:
    metadata = PROVIDER_REGISTRY.get(provider, {})
    capabilities = deepcopy(DEFAULT_MODEL_CAPABILITIES)
    capabilities.update(deepcopy(metadata.get("default_model_capabilities") or {}))

    model_capabilities = metadata.get("model_capabilities") or {}
    normalized_model = normalize_model_id(model)
    normalized_overrides = {
        normalize_model_id(model_id): overrides
        for model_id, overrides in model_capabilities.items()
        if isinstance(overrides, dict)
    }
    capabilities.update(deepcopy(normalized_overrides.get(normalized_model) or {}))
    return capabilities


def get_provider_metadata(provider: LLMProvider) -> dict[str, Any]:
    metadata = deepcopy(PROVIDER_REGISTRY.get(provider, {}))
    if not metadata:
        metadata = {
            "label": provider.value.upper(),
            "default_model": "",
            "models": [],
            "default_endpoint_protocol": "openai_chat",
            "supported_endpoint_protocols": ["openai_chat"],
            "tool_capability": {"tools": False, "tool_choice": []},
        }
    return metadata
