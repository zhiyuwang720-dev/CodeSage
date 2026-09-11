"""LLM service type definitions."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class LLMProvider(str, Enum):
    """Supported LLM providers."""

    GEMINI = "gemini"
    OPENAI = "openai"
    CLAUDE = "claude"
    QWEN = "qwen"
    DEEPSEEK = "deepseek"
    ZHIPU = "zhipu"
    MOONSHOT = "moonshot"
    BAIDU = "baidu"
    MINIMAX = "minimax"
    DOUBAO = "doubao"
    MIMO = "mimo"
    OLLAMA = "ollama"


@dataclass
class LLMConfig:
    """LLM configuration."""

    provider: LLMProvider
    api_key: str
    model: str
    base_url: Optional[str] = None
    timeout: int = 300
    temperature: Optional[float] = None
    max_tokens: int = 4096
    top_p: Optional[float] = None
    frequency_penalty: float = 0
    presence_penalty: float = 0
    endpoint_protocol: str = "openai_compatible"
    tool_message_format: str = "auto"
    custom_headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class LLMUsage:
    """Normalized token usage without conflating missing, zero, and estimates."""

    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    field_sources: Dict[str, str] = field(default_factory=dict)
    estimated_usage: Optional[Dict[str, Any]] = None
    raw_usage: Optional[Dict[str, Any]] = None
    usage_source: str = "unknown"
    normalization_version: str = "2"
    anomalies: List[str] = field(default_factory=list)
    usage_present: bool = True

    @property
    def input_tokens(self) -> Optional[int]:
        return self.prompt_tokens

    @property
    def output_tokens(self) -> Optional[int]:
        return self.completion_tokens

    def to_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "input_tokens": self.prompt_tokens,
            "output_tokens": self.completion_tokens,
            # Compatibility aliases retained while callers migrate to input/output.
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "field_sources": dict(self.field_sources),
            "estimated_usage": dict(self.estimated_usage) if self.estimated_usage else None,
            "usage_source": self.usage_source,
            "normalization_version": self.normalization_version,
            "anomalies": list(self.anomalies),
            "usage_present": self.usage_present,
        }
        if include_raw:
            payload["raw_usage"] = dict(self.raw_usage) if self.raw_usage is not None else None
        return payload


@dataclass
class LLMResponse:
    """LLM response."""

    content: str
    model: Optional[str] = None
    usage: Optional[LLMUsage] = None
    finish_reason: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    reasoning_content: Optional[str] = None
    configured_model: Optional[str] = None
    request_model: Optional[str] = None
    response_model: Optional[str] = None
    provider: Optional[str] = None
    endpoint_id: Optional[str] = None
    protocol: Optional[str] = None
    perspective: Optional[str] = None
    purpose: str = "review"
    provider_request_id: Optional[str] = None
    response_cost_usd: Optional[float] = None
    response_cost_source: Optional[str] = None

    def __post_init__(self) -> None:
        if self.response_model is None and self.model is not None:
            self.response_model = self.model


DEFAULT_MODELS: Dict[LLMProvider, str] = {
    LLMProvider.GEMINI: "gemini-3.5-flash",
    LLMProvider.OPENAI: "gpt-5.5",
    LLMProvider.CLAUDE: "claude-opus-4-8",
    LLMProvider.QWEN: "qwen3.7-max",
    LLMProvider.DEEPSEEK: "deepseek-v4-pro",
    LLMProvider.ZHIPU: "glm-5.2",
    LLMProvider.MOONSHOT: "kimi-k2.6",
    LLMProvider.BAIDU: "ernie-4.5",
    LLMProvider.MINIMAX: "minimax-m2.7",
    LLMProvider.DOUBAO: "doubao-1.6-pro",
    LLMProvider.MIMO: "mimo-v2.5-pro",
    LLMProvider.OLLAMA: "llama3.3-70b",
}


DEFAULT_BASE_URLS: Dict[LLMProvider, str] = {
    LLMProvider.OPENAI: "https://api.openai.com/v1",
    LLMProvider.QWEN: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    LLMProvider.DEEPSEEK: "https://api.deepseek.com",
    LLMProvider.ZHIPU: "https://open.bigmodel.cn/api/paas/v4",
    LLMProvider.MOONSHOT: "https://api.moonshot.cn/v1",
    LLMProvider.BAIDU: "https://aip.baidubce.com/rpc/2.0/ai_custom/v1",
    LLMProvider.MINIMAX: "https://api.minimax.chat/v1",
    LLMProvider.DOUBAO: "https://ark.cn-beijing.volces.com/api/v3",
    LLMProvider.MIMO: "https://api.xiaomimimo.com/v1",
    LLMProvider.OLLAMA: "http://localhost:11434/v1",
    LLMProvider.GEMINI: "https://generativelanguage.googleapis.com/v1beta",
    LLMProvider.CLAUDE: "https://api.anthropic.com/v1",
}
