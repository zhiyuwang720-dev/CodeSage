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
class LLMMessage:
    """LLM request message."""

    role: str
    content: Any
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    reasoning_content: Optional[str] = None

    @classmethod
    def from_dict(cls, item: Dict[str, Any]) -> "LLMMessage":
        return cls(
            role=str(item["role"]),
            content=item.get("content"),
            name=item.get("name"),
            tool_calls=item.get("tool_calls"),
            tool_call_id=item.get("tool_call_id"),
            reasoning_content=item.get("reasoning_content"),
        )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name is not None:
            data["name"] = self.name
        if self.tool_calls is not None:
            data["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.reasoning_content is not None:
            data["reasoning_content"] = self.reasoning_content
        return data


@dataclass
class LLMRequest:
    """LLM request parameters."""

    messages: List[LLMMessage]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    tools: Optional[List[Dict[str, Any]]] = None
    parallel_tool_calls: Optional[bool] = None
    stream: bool = False


@dataclass
class LLMUsage:
    """Token usage."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class LLMResponse:
    """LLM response."""

    content: str
    model: Optional[str] = None
    usage: Optional[LLMUsage] = None
    finish_reason: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    reasoning_content: Optional[str] = None


class LLMError(Exception):
    """LLM error with provider context."""

    def __init__(
        self,
        message: str,
        provider: Optional[LLMProvider] = None,
        status_code: Optional[int] = None,
        original_error: Optional[Any] = None,
        api_response: Optional[str] = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.original_error = original_error
        self.api_response = api_response


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
