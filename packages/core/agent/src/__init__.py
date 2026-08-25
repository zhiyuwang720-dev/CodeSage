"""core/agent/src —— agent 服务内核实现。

导出清单照 参考实现 agent/index.ts 的公共面:AgentRegistry、创建
工厂协议、initiator 作用域、Inbox 投影、消费账目、模型选择、
派发辅助、事件词表扩展。包根 __init__.py 转发这里。
"""

from . import consumed_work, dispatch, invariant, model_selection
from .consumed_work import ConsumedWork, accounts_for_claim, fold_consumed_work
from .dispatch import (
    AGENT_PAYLOAD_FIELDS,
    AgentEventDispatch,
    agent_carrier,
    agent_events,
    assemble_context_for,
    emit_agent_event,
)
from .index import (
    AgentFactory,
    AgentHandle,
    AgentRegistry,
    AgentSetup,
    AgentSetupCommit,
    CreateAgentOptions,
    ResumeAgentOptions,
)
from .inbox import Inbox, InboxNotifications
from .invariant import AgentStatusInvariant
from .model_selection import ModelSelectionRef, install_model_selection
from .runtime_types import (
    AGENT_EVENTS,
    AGENT_SUBJECT_EVENTS,
    AGENT_WATERFALL_EVENTS,
    Agent,
    AgentOptions,
    AgentStatus,
    CancelOptions,
    PreStepDecision,
    RequestErrorAction,
    SessionStartSource,
)
from .types import INBOX_TARGETS, InboxTarget
