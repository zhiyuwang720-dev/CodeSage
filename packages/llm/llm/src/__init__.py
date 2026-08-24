"""llm 包:模型调用的能力接缝 —— 契约在此定义,提供者在外独立演进。

一个插件化 harness 里,「模型」不该是一块铁板:服务的消费者要的
是稳定的调用形状,提供模型能力的一方则千差万别 —— 不同的后端、
不同的协议、不同的版本节奏。能力接缝把两者分开:本包定义服务
契约(请求消息 = 统一的 ContentBlock 形状,调用经 llm 服务发起),
提供者经注册口子接入,各自的演进互不牵制。

本包自包含:ai 层(客户端、类型、适配器、重试、成本、VCR)与
模型配置从 codesage 整体转移而来,不引用旧代码 —— llm 是
「转移后的新家」,不是「对旧家的引用」。

消息形状:ContentBlock 是全系统唯一消息契约,本包不另立新类型;
调用配置是唯一新增形状,因为它回答的是「这次调用用什么模型」,
不属于消息本身。
"""

from .adapters.base import BaseAdapter
from .call_config import LlmCallConfig, call_config_equals
from .client import LLMClient, ModelProfile
from .config import GlobalConfig
from .cost import estimate_cost
from .retry import with_retry
from .service import LLMService, ProviderRegistration
from .token_meter import (
    BLOCK_OVERHEAD,
    CHARS_PER_TOKEN,
    ROLE_OVERHEAD,
    TokenMeter,
    UsageBucket,
    estimate_content,
    estimate_message,
    estimate_request,
    estimate_system_tokens,
    estimate_text,
    usage_tokens,
)
from .types import (
    ContentBlock,
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    StreamEvent,
    ToolSpec,
    Usage,
)

__all__ = [
    "BLOCK_OVERHEAD",
    "BaseAdapter",
    "CHARS_PER_TOKEN",
    "ContentBlock",
    "GlobalConfig",
    "LLMClient",
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "LLMService",
    "LlmCallConfig",
    "Message",
    "ModelProfile",
    "ProviderRegistration",
    "ROLE_OVERHEAD",
    "StreamEvent",
    "TokenMeter",
    "ToolSpec",
    "Usage",
    "UsageBucket",
    "call_config_equals",
    "estimate_content",
    "estimate_cost",
    "estimate_message",
    "estimate_request",
    "estimate_system_tokens",
    "estimate_text",
    "usage_tokens",
    "with_retry",
]
