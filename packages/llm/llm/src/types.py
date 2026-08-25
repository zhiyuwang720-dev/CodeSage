"""Internal message contract — Anthropic-style content blocks (design note #12).

Every provider adapter converts to/from this one shape; the rest of the
harness never sees provider-specific formats. OpenAI tool_calls, DeepSeek
reasoning_content, etc. all land here as blocks or stream events.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


class ToolSchema(TypedDict):
    """一个模型的工具可见 schema(纯字典形状,可被结构化拷贝/冻结)。

    与 ContentBlock 契约正交:装配面(system-prompt/agent-loop)用
    这个形状把工具传给请求,适配器在边界转成 provider 格式。
    """

    name: str
    description: str
    parameters: dict[str, Any]


class ContextSnapshotSection(TypedDict):
    """一条已命名动态上下文贡献(快照 form 的分节),按装配序。"""

    name: str
    text: str


class ContentBlock(BaseModel):
    """One content block: text / thinking / tool_use / tool_result."""

    type: Literal["text", "thinking", "tool_use", "tool_result"]
    text: str | None = None
    id: str | None = None  # tool_use id
    name: str | None = None  # tool_use name
    input: dict[str, Any] | None = None  # tool_use input (parsed)
    tool_use_id: str | None = None  # tool_result: which tool_use this answers
    content: str | list["ContentBlock"] | None = None  # tool_result payload
    is_error: bool = False  # tool_result: execution failed


class Message(BaseModel):
    """A single chat message in the internal contract."""

    role: Literal["user", "assistant", "system"]
    content: str | list[ContentBlock]


class ToolSpec(BaseModel):
    """Tool definition sent to the model (provider-neutral)."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class Usage(BaseModel):
    """Token usage, normalized once at the adapter boundary."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0  # DeepSeek cache hit / Anthropic cache_read
    cache_write_tokens: int = 0  # Anthropic cache_write
    total_tokens: int = 0


class LLMRequest(BaseModel):
    """Request to the model (provider-neutral)."""

    messages: list[Message]
    system: str | None = None  # Anthropic-style separate system; OpenAI gets it as first message
    tools: list[ToolSpec] | None = None
    max_tokens: int = 8192  # DeepSeek cap; 4096 truncates long Chinese replies
    temperature: float | None = None
    top_p: float | None = None
    stream: bool = False
    stop_sequences: list[str] = Field(default_factory=list)


class LLMResponse(BaseModel):
    """Final assistant response in the internal contract."""

    content: list[ContentBlock] = Field(default_factory=list)
    stop_reason: str | None = None  # end_turn / tool_use / stop / length / error
    usage: Usage | None = None
    model: str | None = None
    is_error: bool = False  # provider error surfaced as a message (design note: recoverable)
    error_message: str | None = None
    #: PI-03/S3:本次响应被剥除的残缺 tool_use 块数(length 截断或 partial-JSON)。
    #: 剥除发生在 _drop_truncated_tool_uses;loop 侧(§3.2 形态 1)以此区分
    #: 「截断丢过工具调用」与「纯文本截断」。
    dropped_tool_uses: int = 0

    @property
    def text(self) -> str:
        """Concatenated visible text (excludes thinking and tool blocks)."""
        return "".join(b.text or "" for b in self.content if b.type == "text")


class StreamEvent(BaseModel):
    """One streamed event from any provider (unified)."""

    type: Literal[
        "text_delta",
        "thinking_delta",
        "tool_use_start",
        "tool_use_delta",
        "usage",
        "error",
        "done",
    ]
    text: str | None = None
    thinking: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    input_json_delta: str | None = None  # incremental tool input JSON fragment
    usage: Usage | None = None
    error: str | None = None
    stop_reason: str | None = None


class LLMError(Exception):
    """Provider error with retry semantics attached."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
        original_error: BaseException | None = None,
        cancelled: bool = False,
    ):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.original_error = original_error
        self.cancelled = cancelled

    @classmethod
    def classify(cls, status_code: int) -> bool:
        """408/409/429 and 5xx are retryable; other 4xx are not."""
        return status_code in (408, 409, 429) or status_code >= 500
