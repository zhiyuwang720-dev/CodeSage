"""模型边界配置：不可变请求快照、模型目录与传输族解析（P02/P03）。

本模块是唯一把 CodeSage 配置解析成 LiteLLM SDK 调用参数的位置：
- `LLMConfig` 是每请求快照，除 `api_key` 外可安全序列化与哈希；
- `MODEL_BOUNDARY_VERSION` 参与恢复指纹，旧身份不因新实现被改写；
- `resolve_transport` 只做 SDK model/base_url 映射，不做厂商协议分派。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .types import DEFAULT_BASE_URLS, DEFAULT_MODELS, LLMProvider

MODEL_BOUNDARY_VERSION = "litellm_sdk_v1"

RETRY_OWNER_HARNESS = "harness"
RETRY_OWNER_SDK = "litellm_sdk"

RETRY_BUDGET_HARNESS = 1
RETRY_BUDGET_INDEPENDENT = 3


@dataclass(frozen=True)
class LLMConfig:
    """单次模型请求的不可变配置快照。

    `api_key` 在 `compare=False, repr=False` 下不参与快照比较与调试输出，
    序列化快照使用 `snapshot()`，其输出不含凭据。
    """

    provider: LLMProvider
    api_key: str = field(default="", compare=False, repr=False)
    model: str = ""
    sdk_model: str = ""
    transport: str = "openai_compatible"
    base_url: Optional[str] = None
    timeout: int = 300
    temperature: Optional[float] = None
    max_tokens: int = 4096
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    endpoint_protocol: str = "openai_chat"
    tool_message_format: str = "auto"
    custom_headers: Dict[str, str] = field(default_factory=dict)
    stream_options_include_usage: bool = True
    purpose: str = "review"
    retry_owner: str = RETRY_OWNER_SDK
    retry_budget: int = RETRY_BUDGET_INDEPENDENT
    model_boundary_version: str = MODEL_BOUNDARY_VERSION

    @property
    def configured_model(self) -> str:
        return self.model

    @property
    def endpoint_id(self) -> Optional[str]:
        if not self.base_url:
            return None
        parsed = urlsplit(self.base_url)
        if not parsed.hostname:
            return None
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"

    @property
    def sdk_retries_enabled(self) -> bool:
        return self.retry_owner == RETRY_OWNER_SDK and self.retry_budget > 1

    def snapshot(self) -> Dict[str, Any]:
        """可序列化且不含凭据的请求快照。"""

        return {
            "model_boundary_version": self.model_boundary_version,
            "configured_model": self.model,
            "sdk_model": self.sdk_model,
            "provider": self.provider.value,
            "endpoint_id": self.endpoint_id,
            "base_url": self.base_url,
            "transport": self.transport,
            "endpoint_protocol": self.endpoint_protocol,
            "tool_message_format": self.tool_message_format,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "timeout": self.timeout,
            "purpose": self.purpose,
            "retry_owner": self.retry_owner,
            "retry_budget": self.retry_budget,
            "custom_header_names": sorted(self.custom_headers),
        }


@dataclass
class LLMRequest:
    """一次模型请求（消息与工具已在调用方完成转换）。"""

    messages: List[Dict[str, Any]]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    parallel_tool_calls: Optional[bool] = None
    stream: bool = False
    perspective: Optional[str] = None
    purpose: str = "review"


def with_request_scope(config: LLMConfig, request: LLMRequest) -> LLMConfig:
    """把请求级 purpose/perspective 合并进快照（不改调用者持有的基础配置）。"""

    return replace(
        config,
        purpose=request.purpose or config.purpose,
    )


# --------------------------------------------------------------------------------------
# 模型目录（原 protocols/registry 的纯元数据能力，保留给配置 API 与前端选项）
# --------------------------------------------------------------------------------------

PROVIDER_METADATA: Dict[LLMProvider, Dict[str, Any]] = {
    LLMProvider.OPENAI: {
        "label": "OPENAI",
        "default_model": "gpt-5.5",
        "models": ["gpt-5.6", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "o4-mini", "o3"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none", "required", "forced"], "parallel_tool_calls": True},
        "notes": "OpenAI 走 LiteLLM openai chat 路径；Responses API 选项本轮不可用。",
    },
    LLMProvider.CLAUDE: {
        "label": "CLAUDE",
        "default_model": "claude-opus-4-8",
        "models": ["claude-opus-4-8", "claude-fable-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-sonnet-4-5"],
        "default_endpoint_protocol": "anthropic_messages",
        "supported_endpoint_protocols": ["anthropic_messages"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none", "any", "forced"], "parallel_tool_calls": True},
        "default_model_capabilities": {
            "supports_temperature": True,
            "preferred_finalization_mode": "assistant_prefill",
        },
        "model_capabilities": {
            "claude-opus-4-8": {"supports_temperature": False, "preferred_finalization_mode": "tool_call"},
            "claude-opus-4-7": {"supports_temperature": False, "preferred_finalization_mode": "tool_call"},
            "claude-opus-4-6": {"supports_temperature": False, "preferred_finalization_mode": "tool_call"},
            "claude-sonnet-4-5": {"supports_temperature": True, "preferred_finalization_mode": "assistant_prefill"},
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
        "supported_endpoint_protocols": ["gemini_native"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none", "any", "validated"], "parallel_tool_calls": True},
    },
    LLMProvider.QWEN: {
        "label": "QWEN",
        "default_model": "qwen3.7-max",
        "models": ["qwen3.7-max", "qwen3.7-plus", "qwen3.6-max", "qwen3.6-plus", "qwen3.6-flash", "qwen3-coder-plus", "qwen3-max-instruct"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none"], "parallel_tool_calls": False},
        "notes": "Qwen/百炼的 tool_choice 支持随模型与部署变化，强制工具保持 opt-in。",
    },
    LLMProvider.DEEPSEEK: {
        "label": "DEEPSEEK",
        "default_model": "deepseek-v4-pro",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat"],
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
        "tool_capability": {"tools": True, "tool_choice": ["auto"], "parallel_tool_calls": False},
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
        "default_endpoint_protocol": "unavailable",
        "supported_endpoint_protocols": [],
        "tool_capability": {"tools": False, "tool_choice": []},
        "notes": "本轮不可用：LiteLLM 无对应原生协议，自研 adapter 已删除。",
    },
    LLMProvider.MINIMAX: {
        "label": "MINIMAX",
        "default_model": "minimax-m2.7",
        "models": ["minimax-m3", "minimax-m2.7", "minimax-m2.5", "minimax-m2.1", "minimax-m2"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none"], "parallel_tool_calls": False},
        "notes": "走 openai_compatible 传输族；未列入本轮必测矩阵。",
    },
    LLMProvider.DOUBAO: {
        "label": "DOUBAO",
        "default_model": "doubao-1.6-pro",
        "models": ["doubao-1.6-pro", "doubao-1.5-pro", "doubao-seed-code", "doubao-seed-1.6"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none"], "parallel_tool_calls": False},
        "notes": "走 openai_compatible 传输族；未列入本轮必测矩阵。",
    },
    LLMProvider.MIMO: {
        "label": "MIMO",
        "default_model": "mimo-v2.5-pro",
        "models": ["mimo-v2.5-pro", "mimo-v2.5-flash"],
        "default_endpoint_protocol": "openai_chat",
        "supported_endpoint_protocols": ["openai_chat"],
        "tool_capability": {"tools": True, "tool_choice": ["auto", "none"], "parallel_tool_calls": False, "reasoning_content": True},
        "notes": "按 OpenAI Chat 兼容的 reasoning provider 处理。",
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

PROVIDER_ALIASES = {
    "gemini": LLMProvider.GEMINI,
    "openai": LLMProvider.OPENAI,
    "claude": LLMProvider.CLAUDE,
    "anthropic": LLMProvider.CLAUDE,
    "qwen": LLMProvider.QWEN,
    "dashscope": LLMProvider.QWEN,
    "deepseek": LLMProvider.DEEPSEEK,
    "zhipu": LLMProvider.ZHIPU,
    "glm": LLMProvider.ZHIPU,
    "moonshot": LLMProvider.MOONSHOT,
    "kimi": LLMProvider.MOONSHOT,
    "baidu": LLMProvider.BAIDU,
    "ernie": LLMProvider.BAIDU,
    "minimax": LLMProvider.MINIMAX,
    "doubao": LLMProvider.DOUBAO,
    "volcengine": LLMProvider.DOUBAO,
    "mimo": LLMProvider.MIMO,
    "xiaomimimo": LLMProvider.MIMO,
    "ollama": LLMProvider.OLLAMA,
}

# 旧 endpoint_protocol 取值 → 规范值；`unavailable` 表示本轮无 SDK 传输族。
PROTOCOL_CANONICAL: Dict[str, str] = {
    "openai": "openai_chat",
    "openai_chat": "openai_chat",
    "openai_compatible": "openai_chat",
    "openai-compatible": "openai_chat",
    "chat_completions": "openai_chat",
    "chat_completion": "openai_chat",
    "anthropic": "anthropic_messages",
    "anthropic_messages": "anthropic_messages",
    "claude": "anthropic_messages",
    "google": "gemini_native",
    "gemini": "gemini_native",
    "gemini_native": "gemini_native",
    "openai_responses": "unavailable",
    "responses": "unavailable",
    "native": "unavailable",
}

# 规范 endpoint_protocol → 统一 SDK 传输族。
ENDPOINT_PROTOCOL_TRANSPORTS: Dict[str, Optional[str]] = {
    "openai_chat": "openai_compatible",
    "anthropic_messages": "anthropic_messages",
    "gemini_native": "gemini",
    "unavailable": None,
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
    "legacy": "legacy_text",
    "legacy_text": "legacy_text",
    "xml": "legacy_text",
    "json": "legacy_text",
}


class ModelConfigurationError(ValueError):
    """配置在发送前即不可用；错误信息面向配置修正，不含凭据。"""


def parse_provider(value: str | None) -> LLMProvider:
    return PROVIDER_ALIASES.get(str(value or "").strip().lower(), LLMProvider.OPENAI)


def canonical_endpoint_protocol(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "openai_chat"
    canonical = PROTOCOL_CANONICAL.get(raw)
    if canonical is None:
        raise ModelConfigurationError(f"未知的 endpoint_protocol: {raw}")
    if canonical == "unavailable":
        raise ModelConfigurationError(
            f"endpoint_protocol={raw} 在本版本不可用；LiteLLM SDK 无对应传输族，请改用 openai_chat 或 anthropic_messages"
        )
    return canonical


def protocol_transport(value: str | None) -> Optional[str]:
    """返回传输族；`None` 表示该协议本轮不可用。未知协议按规范族默认处理。"""

    raw = str(value or "").strip().lower()
    if not raw:
        return "openai_compatible"
    return ENDPOINT_PROTOCOL_TRANSPORTS.get(PROTOCOL_CANONICAL.get(raw, raw), None)


def canonical_tool_message_format(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    return TOOL_FORMAT_ALIASES.get(raw, raw or "auto")


def resolve_tool_message_format(
    endpoint_protocol: str | None, *, provider: str | None = None, requested: str | None = "auto"
) -> str:
    """Harness transcript 到 SDK 消息的 tool 表示；不再承担厂商协议分派。"""

    explicit = canonical_tool_message_format(requested)
    if explicit != "auto":
        return explicit

    transport = protocol_transport(endpoint_protocol)
    if transport == "anthropic_messages":
        return "anthropic_blocks"
    if transport == "gemini":
        return "gemini_parts"
    if transport is None:
        raise ModelConfigurationError(f"endpoint_protocol={endpoint_protocol} 在本版本不可用")
    if str(provider or "").strip().lower() in {"claude", "anthropic"} and transport != "openai_compatible":
        return "anthropic_blocks"
    return "openai_tools"


def normalize_model_id(model: str | None) -> str:
    return str(model or "").strip().lower().replace(".", "-")


def get_provider_metadata(provider: LLMProvider) -> Dict[str, Any]:
    metadata = PROVIDER_METADATA.get(provider)
    if not metadata:
        return {
            "label": provider.value.upper(),
            "default_model": DEFAULT_MODELS.get(provider, ""),
            "models": [],
            "default_endpoint_protocol": "openai_chat",
            "supported_endpoint_protocols": ["openai_chat"],
            "tool_capability": {"tools": False, "tool_choice": []},
        }
    return {
        **metadata,
        "default_endpoint_protocol": _public_protocol(metadata.get("default_endpoint_protocol")),
        "supported_endpoint_protocols": [
            protocol
            for protocol in metadata.get("supported_endpoint_protocols", [])
            if protocol_transport(protocol) is not None
        ],
    }


def _public_protocol(value: Optional[str]) -> Optional[str]:
    if value and protocol_transport(value) is None:
        return None
    return value


def get_model_capabilities(provider: LLMProvider, model: str | None) -> Dict[str, Any]:
    metadata = PROVIDER_METADATA.get(provider) or {}
    capabilities: Dict[str, Any] = {
        "supports_temperature": True,
        "supports_assistant_prefill": True,
        "preferred_finalization_mode": "assistant_prefill",
    }
    capabilities.update(dict(metadata.get("default_model_capabilities") or {}))
    overrides = {
        normalize_model_id(model_id): value
        for model_id, value in (metadata.get("model_capabilities") or {}).items()
        if isinstance(value, dict)
    }
    capabilities.update(dict(overrides.get(normalize_model_id(model)) or {}))
    return capabilities


class ModelCatalog:
    """配置 API 使用的只读模型目录。"""

    @staticmethod
    def supported_providers() -> List[LLMProvider]:
        return list(LLMProvider)

    @staticmethod
    def provider_metadata(provider: LLMProvider) -> Dict[str, Any]:
        return get_provider_metadata(provider)

    @staticmethod
    def default_model(provider: LLMProvider) -> str:
        return str(get_provider_metadata(provider).get("default_model") or DEFAULT_MODELS.get(provider, ""))

    @staticmethod
    def available_models(provider: LLMProvider) -> List[str]:
        return list(get_provider_metadata(provider).get("models") or [])


def sdk_provider_prefix(provider: LLMProvider) -> Optional[str]:
    """返回该 provider 在 LiteLLM 中的显式前缀；None 表示靠 api_base 判定的兼容族。

    只有走厂商原生协议的 provider 才使用厂商前缀（anthropic / gemini / ollama）。
    OpenAI Chat Completions 兼容端一律使用 `openai/`：厂商前缀会让 SDK 选择
    厂商专属流解析器（LiteLLM 1.98 的 deepseek 流路径会把 HTTP chunk 边界当 SSE
    行读取），而 wire 协议本来就是 OpenAI 兼容。
    """

    mapping = {
        LLMProvider.CLAUDE: "anthropic",
        LLMProvider.GEMINI: "gemini",
        LLMProvider.OLLAMA: "ollama",
        LLMProvider.OPENAI: "openai",
    }
    return mapping.get(provider)

# OpenAI 兼容协议族统一使用的 SDK provider 前缀。
OPENAI_COMPATIBLE_PREFIX = "openai"


KNOWN_LITELLM_PREFIXES = {
    "openai", "anthropic", "gemini", "deepseek", "ollama", "azure", "huggingface", "together",
    "groq", "mistral", "anyscale", "replicate", "bedrock", "vertex_ai", "cohere", "sagemaker",
    "palm", "ai21", "nlp_cloud", "aleph_alpha", "petals", "baseten", "vllm", "cloudflare",
    "xinference", "openrouter", "fireworks_ai", "xai", "dashscope", "moonshot", "zhipu",
}


def resolve_sdk_model(provider: LLMProvider, model: str, base_url: Optional[str]) -> tuple[str, str]:
    """把配置模型映射成 SDK 请求模型与实际传输族。

    返回值 `(sdk_model, transport)`：
    - 已带 LiteLLM 已知前缀的模型名保持原样（不偷偷更换模型）；
    - OpenAI 兼容族统一用 `openai/<model>` 前缀 + `api_base`；
    - 厂商原生协议使用对应 SDK provider 前缀（anthropic / gemini / ollama）。
    """

    raw = str(model or "").strip()
    if not raw:
        raise ModelConfigurationError("模型名称为空：请在配置中指定模型")

    if "/" in raw:
        prefix = raw.split("/", 1)[0].strip().lower()
        if prefix in KNOWN_LITELLM_PREFIXES:
            return raw, _transport_for_prefix(prefix)
        if base_url:
            # 例如 SiliconFlow 的 "Qwen/Qwen3-8B"：端点已给出，按 OpenAI 兼容发送。
            return f"{OPENAI_COMPATIBLE_PREFIX}/{raw}", "openai_compatible"
        explicit = sdk_provider_prefix(provider)
        if explicit:
            return f"{explicit}/{raw}", _transport_for_prefix(explicit)
        raise ModelConfigurationError(
            f"模型名 {raw} 带未知前缀且未配置 base_url；请显式配置 endpoint 或使用 provider/model 形式"
        )

    explicit = sdk_provider_prefix(provider)
    if explicit:
        return f"{explicit}/{raw}", _transport_for_prefix(explicit)

    if not base_url:
        raise ModelConfigurationError(
            f"provider={provider.value} 需要显式 base_url 才能确定请求端点；请在配置中提供 base_url"
        )
    return f"{OPENAI_COMPATIBLE_PREFIX}/{raw}", "openai_compatible"


def _transport_for_prefix(prefix: str) -> str:
    if prefix == "anthropic":
        return "anthropic_messages"
    if prefix == "gemini":
        return "gemini"
    if prefix == "ollama":
        return "ollama"
    return "openai_compatible"


def default_base_url(provider: LLMProvider) -> Optional[str]:
    return DEFAULT_BASE_URLS.get(provider)
