"""llm 包:模型调用的能力接缝 —— 契约在此定义,提供者在外独立演进。

一个插件化 harness 里,「模型」不该是一块铁板:服务的消费者要的
是稳定的调用形状,提供模型能力的一方则千差万别 —— 不同的后端、
不同的协议、不同的版本节奏。能力接缝把两者分开:本包定义服务
契约(请求消息 = 统一的 ContentBlock 形状,调用经 llm 服务发起),
提供者经注册口子接入,各自的演进互不牵制。

本包自包含:ai 层(客户端、类型、适配器、重试、成本、VCR)与

消息形状:ContentBlock 是全系统唯一消息契约,本包不另立新类型;
调用配置是唯一新增形状,因为它回答的是「这次调用用什么模型」,
不属于消息本身。
"""

from .adapters.base import BaseAdapter
from .assembler import BlockAssembler
from .call_config import LlmCallConfig, call_config_equals
from .client import LLMClient, ModelProfile
from .config import GlobalConfig
from .cost import estimate_cost
from .error_chain import (
    CONTEXT_WINDOW_EXCEEDED_CODE,
    EMPTY_RESPONSE_CODE,
    HarnessError,
    INVALID_CREDENTIAL_CODE,
    QUOTA_EXCEEDED_CODE,
    error_chain,
    is_context_window_exceeded_error,
    is_harness_error,
    is_quota_exceeded_error,
)
from .loop_markers import is_agent_loop_request, mark_agent_loop_request
from .messages import (
    CONTEXT_SUMMARY_MAX_CHARS,
    MessageId,
    bound_context_summary,
    create_assistant_message,
    create_message,
    create_tool_result_message,
    create_user_message,
    is_token_delta,
)
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
    ContextSnapshotSection,
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    StreamEvent,
    ToolSchema,
    ToolSpec,
    Usage,
)

__all__ = [
    "BLOCK_OVERHEAD",
    "BaseAdapter",
    "BlockAssembler",
    "CHARS_PER_TOKEN",
    "CONTEXT_SUMMARY_MAX_CHARS",
    "CONTEXT_WINDOW_EXCEEDED_CODE",
    "ContentBlock",
    "EMPTY_RESPONSE_CODE",
    "GlobalConfig",
    "HarnessError",
    "INVALID_CREDENTIAL_CODE",
    "LLMClient",
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "LLMService",
    "LlmCallConfig",
    "Message",
    "MessageId",
    "ModelProfile",
    "ProviderRegistration",
    "QUOTA_EXCEEDED_CODE",
    "ROLE_OVERHEAD",
    "StreamEvent",
    "TokenMeter",
    "ContextSnapshotSection",
    "ToolSchema",
    "ToolSpec",
    "Usage",
    "UsageBucket",
    "bound_context_summary",
    "call_config_equals",
    "create_assistant_message",
    "create_message",
    "create_tool_result_message",
    "create_user_message",
    "error_chain",
    "estimate_content",
    "estimate_cost",
    "estimate_message",
    "estimate_request",
    "estimate_system_tokens",
    "estimate_text",
    "is_agent_loop_request",
    "is_context_window_exceeded_error",
    "is_harness_error",
    "is_quota_exceeded_error",
    "is_token_delta",
    "mark_agent_loop_request",
    "usage_tokens",
    "with_retry",
]

# 双名前缀桥:同一份源码可能从 "llm.src"(浅路径,家族内适配器
# 习惯)或 "llm.llm.src"(深路径,跨包消费方习惯)加载 —— 目录
# 只有一个,模块对象却会因名字不同加载两份,类身份随之分裂
# (pydantic 模型校验直接炸)。这里把另一前缀注册为别名,连同
# 已加载的子模块,两套名字永远指向同一份对象。
import sys as _sys

if __name__.startswith("llm.llm."):
    _other_prefix = "llm" + __name__[len("llm.llm"):]
else:
    _other_prefix = "llm.llm" + __name__[len("llm"):]
_sys.modules[_other_prefix] = _sys.modules[__name__]
for _sub_name, _sub_mod in list(_sys.modules.items()):
    if _sub_name.startswith(__name__ + "."):
        _sys.modules[_other_prefix + _sub_name[len(__name__):]] = _sub_mod
