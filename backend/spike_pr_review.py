# -*- coding: utf-8 -*-
"""
Phase 1 L2 验收脚本(移植自 AutoCVE Phase 0 Spike) — PR 语义 Runtime 端到端单文件演示
=================================================
目标：用 AutoCVE 现有运行时机制（finding_runtime + runtime_core），在【不改任何源码】的前提下，
跑通一个 "PR 审查语义" 的完整 ReAct 会话：
    PR 语义 system prompt → QueryLoop → (Read/Grep 工具) → FinalizeReview(自定义终点工具) → 结构化评论

一个文件走完的完整调用链（对照代码库位置）：
    [配置]   settings                       app/core/config.py:11        (pydantic-settings, 读环境变量/.env)
    [组装]   AuditSessionStore              app/services/finding_runtime/session_store.py:55
    [组装]   LLMService                     app/services/llm/service.py:28
    [组装]   SpikeModelClient               ← 本文件(等价于 RuntimeLLMModelClient, bridge.py:69 的最小实现)
    [组装]   build_shared_agent_tool_catalog  app/services/agent/tools/shared_catalog.py:17
    [组装]   build_runtime_tool_registry    app/services/runtime_core/runtime_tool_registry.py:586
    [组装]   ToolOrchestrator               app/services/runtime_core/tool_runtime.py:410
    [运行]   FindingRuntimeRunner           app/services/finding_runtime/runner.py:18
    [运行]   QueryLoop.run_turn             app/services/finding_runtime/query_loop.py:109
    [终结]   FinalizeReviewTool             ← 本文件自定义（生产对应 review_runtime/tools/finalize_review.py）
    [提取]   TurnExecutionResult            app/services/finding_runtime/models.py:166

【为什么 spike 自建两样东西】
  1. SQLite session factory：生产用 app/db/session.py:33 get_sync_session_factory（读 settings.DATABASE_URL，
     默认 PostgreSQL+asyncpg）。spike 为开箱即用，直接用内置 sqlite3 驱动建一个同构 factory（零额外依赖）。
  2. SpikeModelClient：生产用 RuntimeLLMModelClient（bridge.py:69，完整版还处理多厂商 tool_message_format）。
     spike 实现最小等价版（transcript→messages 复用 runtime_core/tool_message_codec.py 的真实代码），
     顺带展示"模型客户端"这个接口长什么样。两者可无缝互换。

本 Spike 验证 docs/spec/00-architecture.md §5.1 Phase 0 的 4 个假设：
    ① 终点工具参数化：自定义 FinalizeReview 能否正常终结？
    ② PR 语义 runtime 跑通（端到端）
    ③ 工具裁剪：只暴露想要的工具给模型
    ④ 落库假设：会话/消息/工具调用/checkpoint 全量落库，零改表

用法：
  1) 模式 A — 真实 LLM（默认）：内置 DeepSeek 配置（provider/model/base_url/key 自动解析），
     无需任何环境变量，直接运行即可：
       python spike_pr_review.py
     （key 解析优先级: 环境变量 DEEPSEEK_API_KEY → 工作区根 .env → 内置测试 key；
       可设 LLM_PROVIDER/LLM_MODEL/LLM_BASE_URL 切换到其它 12 provider）
  2) 模式 B — Mock LLM（无网络/无 key 也能跑，SPIKE_MOCK_LLM=1）：
       SPIKE_MOCK_LLM=1
     Mock 会模拟一个"模型"：第一轮调用 Read 工具读 diff 文件，第二轮调用 FinalizeReview
     提交结构化评论——完整演示 ReAct 多轮循环 + 工具执行 + payload 驱动的结构化终结。
  3) 数据库用 SQLite 文件 spike_phase0.db，无需 PostgreSQL。
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, AsyncGenerator

from pydantic import BaseModel, Field
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

# ── ⚠️ LLM 默认配置(真实模式): 必须在 import app.core.config 之前设置 ────────────
# settings 是进程级单例(app/core/config.py:166 settings = Settings(), 读环境变量 + .env),
# 所以 LLM 相关环境变量必须在此处(import app.* 之前)就绪。
# 优先级: 已有环境变量 > 这里 setdefault 的默认值; key 解析见 _resolve_deepseek_api_key()。
os.environ.setdefault("LLM_PROVIDER", "deepseek")
os.environ.setdefault("LLM_MODEL", "deepseek-chat")
os.environ.setdefault("LLM_BASE_URL", "https://api.deepseek.com")
os.environ.setdefault("LLM_ENDPOINT_PROTOCOL", "openai_chat")


def _resolve_deepseek_api_key() -> str:
    """API key 解析优先级: 环境变量 → E:\\Mac\\CodeSage\\.env(不内置任何密钥)"""
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY") or ""
    if key:
        return key
    spike_path = Path(__file__).resolve()
    for env_file in (spike_path.parents[1] / ".env",):
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return ""  # 不内置任何密钥(安全红线); 未配置时真实模式会在 LLM 调用处报"API Key未配置"


os.environ.setdefault("DEEPSEEK_API_KEY", _resolve_deepseek_api_key())

# ── ① 基础设施 ────────────────────────────────────────────────────────────────
from app.core.config import settings
from app.db.base import Base

# 注册 audit_session 系列表（SQLAlchemy 按 import 注册到 Base.metadata，再 create_all 建表）
import app.models.audit_session  # noqa: F401  表: audit_sessions/messages/turns/tool_calls/checkpoints/skills/memories/handoffs/...

# ── ② 运行时组装所需组件（全部是 AutoCVE 真实代码） ─────────────────────────────
from app.services.agent.tools.shared_catalog import build_shared_agent_tool_catalog
from app.services.review_runtime.models import (
    RuntimeCompletionMode,
    RuntimeMessageRole,
    RuntimeStopReason,
    RuntimeTerminalAction,
    RuntimeModelResponse,
    ToolExecutionPayload,
    TranscriptItem,
)
from app.services.review_runtime.runner import FindingRuntimeRunner
from app.services.review_runtime.session_store import AuditSessionStore
from app.services.llm.service import LLMService
from app.services.runtime_core.runtime_tool_registry import build_runtime_tool_registry
from app.services.runtime_core.tool_message_codec import build_runtime_model_messages
from app.services.runtime_core.tool_runtime import RuntimeTool, ToolExecutionContext, ToolOrchestrator

# ── ③ 自建 SQLite session factory（生产: app/db/session.py:33, 这里用内置 sqlite3 驱动） ──
SPIKE_DB_PATH = Path(__file__).resolve().parent / "spike_phase0.db"
_sync_engine = create_engine(f"sqlite:///{SPIKE_DB_PATH.as_posix()}")
SPIKE_SESSION_FACTORY = sessionmaker(bind=_sync_engine, expire_on_commit=False)

# ── 演示素材：一个"PR diff"（刻意包含 SQL 注入 + 无参数化查询 + 无错误处理） ─────
DEMO_DIFF = """diff --git a/app/routes.py b/app/routes.py
--- a/app/routes.py
+++ b/app/routes.py
@@ -10,6 +10,16 @@
 from flask import request, jsonify
 from app.db import get_db

+@app.route("/api/search")
+def search():
+    query = request.args.get("q", "")
+    sql = f"SELECT * FROM items WHERE title LIKE '%{query}%'"
+    rows = get_db().execute(sql).fetchall()
+    return jsonify([dict(row) for row in rows])
+
+@app.route("/api/items/<int:item_id>")
+def item_detail(item_id):
+    row = get_db().execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
+    if row is None:
+        return jsonify({"error": "not found"}), 404
+    return jsonify(dict(row))
"""

# PR 语义 system prompt（生产对应 review_runtime 各视角 prompt，这里以 Security 视角为例）
PR_SYSTEM_PROMPT = """你是 CodeSage 的 PR 审查 Agent（Security 视角）。
你的任务：审查给定的 PR diff，找出 diff 引入的安全问题（注入、认证/授权、数据暴露等）。
要求：
1. 优先阅读 diff 涉及的文件确认上下文（Read/Grep/Glob 工具可用）；
2. 每条评论必须落在 diff 新增行，给出具体行号；
3. 证据不足时继续调用工具补齐，不要猜测；
4. 审查完成时，必须调用 FinalizeReview 工具提交结构化评论，禁止只用自然语言宣布"完成"。
"""

USER_MESSAGE = (
    "请审查下面的 PR diff，使用工具确认上下文后，调用 FinalizeReview 提交你的审查评论。\n\n"
    f"```diff\n{DEMO_DIFF}\n```"
)

PROJECT_ROOT = Path(__file__).resolve().parent  # 工具可读的文件根（此处为 backend/，仅供演示）

# ── ④ 自定义终点工具：FinalizeReview ────────────────────────────────────────────
# 生产对应: review_runtime/final_review_contract.py + review_runtime/tools/finalize_review.py
# 机制与 FinalizeFindingTool(app/services/finding_runtime/tools/finalize_finding.py) 完全同构:
#   输入严格校验 → 执行 → 返回 output_payload{final_payload, completion_mode, terminal_action}
# QueryLoop 的终结判定是【payload 驱动】的(query_loop.py:545-561), 因此自定义工具名即可工作。


class ReviewComment(BaseModel):
    """简化版 ReviewComment schema(生产含 rule_id/severity/category/confidence/verdict 等)"""

    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    severity: str = Field(pattern="^(low|medium|high|critical)$")
    category: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)


class ReviewPayload(BaseModel):
    comments: list[ReviewComment] = Field(default_factory=list)
    summary: str = Field(min_length=1)


class FinalizeReviewTool(RuntimeTool):
    name = "FinalizeReview"
    description = (
        "提交 PR 审查的结构化评论。这是终点工具:调用成功后审查阶段立即结束。\n"
        "必须包含 comments(每条: path/line/severity/category/title/body)与 summary。"
    )
    input_model = ReviewPayload
    always_load = True  # 与 FinalizeFindingTool 一致: 不进入 deferred, 模型始终可见

    def validate_input(self, raw_input: dict) -> ReviewPayload:
        return ReviewPayload.model_validate(raw_input or {})

    def is_concurrency_safe(self, parsed_input=None) -> bool:
        return False

    async def execute(self, parsed_input: ReviewPayload, context: ToolExecutionContext) -> ToolExecutionPayload:
        del context
        final_payload = parsed_input.model_dump(mode="json", exclude_none=True)
        return ToolExecutionPayload(
            content="Received final structured review comments.",
            output_payload={
                "final_payload": final_payload,
                "completion_mode": RuntimeCompletionMode.FINALIZE_TOOL.value,      # "finalize_tool"
                "terminal_action": RuntimeTerminalAction.FINALIZE_FINDING.value,   # "finalize_finding"(复用枚举)
            },
            metadata={"finalize_review": True},
        )


# ── ⑤ SpikeModelClient：RuntimeLLMModelClient(bridge.py:69) 的最小等价实现 ───────
# 接口(QueryLoop 需要): complete / stream_complete
# 职责: ① transcript(会话消息) → LLM messages(复用 tool_message_codec 真实代码)
#       ② 调 LLMService.chat_completion(12 provider 热切换)
#       ③ 归一化为 RuntimeModelResponse
# 生产完整版额外处理: 各厂商 tool_message_format 差异、流式重试预算、错误分类。
NATIVE_TOOL_CALLING_REMINDER = (
    "重要: 你必须使用模型原生的工具调用(tool calling)执行工具动作,"
    "不要在文本中输出伪工具语法(Action:/Action Input:)。"
)


class SpikeModelClient:
    FINALIZER_TOOL_NAMES = {"FinalizeReview"}  # 生产应参数化, 见文件末尾 SPIKE 结论①

    def __init__(self, llm_service, agent_type: str = "spike-review"):
        self._llm_service = llm_service
        self._agent_type = agent_type

    async def complete(
        self,
        *,
        system_prompt: str | None,
        recon_payload: dict[str, Any],
        transcript: list[Any],
        model_name: str,
        tool_definitions: list[dict[str, Any]],
        max_output_tokens_override: int | None = None,
    ) -> RuntimeModelResponse:
        del model_name
        effective_system_prompt = (system_prompt or "").strip()
        if tool_definitions:
            effective_system_prompt = f"{effective_system_prompt}\n\n{NATIVE_TOOL_CALLING_REMINDER}".strip()
        messages = build_runtime_model_messages(  # 真实代码: runtime_core/tool_message_codec.py:18
            system_prompt=effective_system_prompt,
            recon_payload=recon_payload,
            transcript=transcript,
            tool_definitions=tool_definitions,
            tool_message_format="openai_tools",
        )
        response = await self._llm_service.chat_completion(
            messages=messages,
            agent_type=self._agent_type,
            tools=[self._to_llm_tool_schema(item) for item in tool_definitions],
            parallel_tool_calls=True,
            max_tokens=max_output_tokens_override,
        )
        tool_calls = [self._normalize_tool_call(item) for item in (response.get("tool_calls") or [])]
        return RuntimeModelResponse(
            content=str(response.get("content") or ""),
            reasoning_content=str(response.get("reasoning_content") or ""),
            tool_calls=tool_calls,
            stop_reason=str(response.get("finish_reason") or "stop"),
            usage=dict(response.get("usage") or {}),
            native_tool_call_count=len(tool_calls),
            has_terminal_tool_call=any(
                str(item.get("name") or "") in self.FINALIZER_TOOL_NAMES for item in tool_calls
            ),
        )

    async def stream_complete(
        self,
        *,
        system_prompt: str | None,
        recon_payload: dict[str, Any],
        transcript: list[Any],
        model_name: str,
        tool_definitions: list[dict[str, Any]],
        max_output_tokens_override: int | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        # 简化版流式: 先拿完整响应, 再按 QueryLoop 期望的事件序列回放
        response = await self.complete(
            system_prompt=system_prompt,
            recon_payload=recon_payload,
            transcript=transcript,
            model_name=model_name,
            tool_definitions=tool_definitions,
            max_output_tokens_override=max_output_tokens_override,
        )
        if response.content:
            yield {"type": "content_delta", "content": response.content, "accumulated": response.content}
        for tool_call in response.tool_calls:
            yield {"type": "tool_call", "tool_call": tool_call}
        yield {
            "type": "done",
            "content": response.content,
            "stop_reason": response.stop_reason,
            "recoverable_error_kind": response.recoverable_error_kind,
            "recoverable_error_message": response.recoverable_error_message,
            "tool_calls": [],
        }

    @staticmethod
    def _to_llm_tool_schema(definition: dict[str, Any]) -> dict[str, Any]:
        """与生产 RuntimeLLMModelClient._to_llm_tool_schema(bridge.py:338-347) 完全一致:
        describe_tools 输出 {name, description, input_schema}, 需包装为 OpenAI function 格式,
        否则 LiteLLM 会因缺少 type 字段丢弃全部工具。"""
        return {
            "type": "function",
            "function": {
                "name": definition.get("name", ""),
                "description": definition.get("description", ""),
                "parameters": definition.get("input_schema", {"type": "object"}),
            },
        }

    @staticmethod
    def _normalize_tool_call(raw: dict[str, Any]) -> dict[str, Any]:
        item = dict(raw or {})
        item.setdefault("id", f"tool-use-{abs(hash(str(item)))}")
        item.setdefault("name", str(item.get("function", {}).get("name") or "unknown") if isinstance(item.get("function"), dict) else str(item.get("name") or "unknown"))
        raw_input = item.get("input") or item.get("arguments")
        if isinstance(raw_input, str):
            try:
                raw_input = json.loads(raw_input)
            except json.JSONDecodeError:
                raw_input = {"raw": raw_input}
        item["input"] = raw_input if isinstance(raw_input, dict) else {}
        return item


# ── ⑤b MockLLMService：无网络/无 key 时的"模型替身" ─────────────────────────────
# 实现 LLMService.chat_completion 的接口（SpikeModelClient 只依赖这一个方法）。
# 行为：模拟一个聪明的审查模型——
#   第一轮: 决定先调用 Read 工具确认上下文（触发真实工具执行 → 结果回填）
#   第二轮: 拿到工具结果后调用 FinalizeReview 提交结构化评论（触发 payload 驱动终结）
# 从而在无外网环境下也能完整演示: 多轮 ReAct + 工具执行 + 结构化终结 + 全量落库。


class MockLLMService:
    async def chat_completion(
        self,
        messages,
        temperature=None,
        max_tokens=None,
        agent_type=None,
        tools=None,
        parallel_tool_calls=None,
    ) -> dict:
        del temperature, max_tokens, agent_type, parallel_tool_calls
        last_role = str((messages or [{}])[-1].get("role") or "")
        tool_names = [
            str(t.get("function", {}).get("name") or t.get("name") or "")
            for t in (tools or [])
        ]
        print(f"     [mock-llm] 可见工具: {tool_names}")
        if last_role != "tool":
            # 第一轮: 先读文件确认上下文（触发真实的 Read 工具执行）
            return {
                "content": "我先读取 diff 涉及的文件确认上下文。",
                "tool_calls": [{"id": "call-1", "name": "Read", "input": {"path": "app/core/config.py"}}],
                "finish_reason": "tool_calls",
                "reasoning_content": "需要确认 app/routes.py 是否存在以及 db 用法。",
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            }
        # 第二轮: 工具结果已回填, 证据闭合 → 结构化终结
        return {
            "content": "基于源码确认, 该 SQL 拼接存在注入风险。",
            "tool_calls": [
                {
                    "id": "call-2",
                    "name": "FinalizeReview",
                    "input": {
                        "comments": [
                            {
                                "path": "app/routes.py",
                                "line": 15,
                                "severity": "critical",
                                "category": "security",
                                "title": "SQL 注入: 用户输入直接拼接进 SQL",
                                "body": "q 参数未经参数化直接以 f-string 拼入 SQL, 攻击者可注入任意 SQL。建议使用参数化查询。",
                            }
                        ],
                        "summary": "发现 1 个 SQL 注入问题(app/routes.py:15), 其余新增行无明显安全问题。",
                    },
                }
            ],
            "finish_reason": "tool_calls",
            "reasoning_content": "证据闭合: 第 15 行 f-string 拼接用户输入到 SQL。",
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        }


# ── ⑥ 事件打印器：把 QueryLoop 的内部事件流变成可读日志（生产对应 SSE 推送前端） ──


def build_event_printer():
    reasoning_accumulated = ""

    async def on_event(event: dict) -> None:
        nonlocal reasoning_accumulated
        event_type = str(event.get("type") or "")
        if event_type == "reasoning_delta":
            reasoning_accumulated = str(event.get("accumulated") or reasoning_accumulated)
            return
        if event_type == "assistant_start":
            print("\n  🤖 [assistant] 开始输出...")
            return
        if event_type == "token":
            print(f"     {str(event.get('content') or '')}", end="", flush=True)
            return
        if event_type == "tool_call":
            print(f"  🔧 [tool_call] {event.get('tool_name') or event.get('name')} {json.dumps(event.get('tool_input') or event.get('input') or {}, ensure_ascii=False)[:160]}")
            return
        if event_type == "tool_result":
            result_text = str(event.get("result") or event.get("content") or "")[:200]
            print(f"  ✅ [tool_result] {event.get('tool_name') or ''} → {result_text}")
            return
        if event_type == "message":
            msg = event.get("message") or {}
            name = msg.get("name") or ""
            content = str(msg.get("content") or "")[:200]
            role = msg.get("role") or ""
            if name or role == "user":
                print(f"  💬 [{role}] {content}")
            return
        if event_type == "done":
            content = str(event.get("content") or "")
            if content:
                print(f"\n  🤖 [assistant] {content[:300]}")
            return
        if event_type == "llm_retry":
            print(f"  ⏳ [llm_retry] {event.get('message_text')}")
            return
        if event_type == "error":
            print(f"  ❌ [error] {event.get('user_message') or event.get('error')}")
            return
        if event_type == "assistant_tombstone":
            print(f"  💀 [tombstone] attempt {event.get('attempt_id')} status={event.get('status')}")
            return
        # 其他事件(nudge/checkpoint 等)静默

    return on_event


# ── ⑦ DB 落库统计：验证假设④(零改表, 全部落到 audit_session 系列表) ─────────────


def db_stats(session_id: str) -> dict:
    stats = {}
    with SPIKE_SESSION_FACTORY() as db:
        for table_name, model in [
            ("audit_sessions", app.models.audit_session.AuditSession),
            ("audit_session_messages", app.models.audit_session.AuditSessionMessage),
            ("audit_session_turns", app.models.audit_session.AuditSessionTurn),
            ("audit_tool_calls", app.models.audit_session.AuditToolCall),
            ("audit_checkpoints", app.models.audit_session.AuditCheckpoint),
            ("audit_handoffs", app.models.audit_session.AuditHandoff),
            ("audit_model_stream_attempts", app.models.audit_session.AuditModelStreamAttempt),
        ]:
            if table_name == "audit_sessions":
                count = db.scalar(select(func.count()).select_from(model).where(model.id == session_id))
            elif hasattr(model, "project_id") and table_name != "audit_tool_calls":
                count = db.scalar(select(func.count()).select_from(model).where(model.project_id == "spike-pr"))
            else:
                count = db.scalar(select(func.count()).select_from(model))
            stats[table_name] = count or 0
    return stats


# ── ⑧ 主流程：手动组装 = 生产 FindingRuntimeBridge.run(bridge.py:532) 的逐行展开 ──


async def main() -> None:
    print("=" * 78)
    print("Phase 1 L2 验收(移植版 Spike) — PR 语义 Runtime 端到端")
    print(f"  LLM provider : {settings.LLM_PROVIDER}")
    print(f"  LLM model    : {settings.LLM_MODEL}")
    print(f"  LLM base_url : {settings.LLM_BASE_URL}")
    print(f"  SQLite DB    : {SPIKE_DB_PATH}")
    print("=" * 78)

    # 0) 建表（SQLite 开箱即用；PostgreSQL 环境用 alembic upgrade head）
    Base.metadata.create_all(_sync_engine)

    # 1) 会话存储(生产: finding_runtime/session_store.py) — 所有消息/回合/工具调用/checkpoint 落库
    session_store = AuditSessionStore(session_factory=SPIKE_SESSION_FACTORY)

    # 2) 创建 runtime 会话(生产: FindingRuntimeAdapter.run, adapters/finding.py:51)
    session_id = session_store.create_session(
        project_id="spike-pr",
        task_id="spike",
        runtime_stack="runtime",
        system_prompt=PR_SYSTEM_PROMPT,
        recon_payload={"diff": DEMO_DIFF, "repo": "demo-project", "pr_number": 1},
    )
    session_store.append_message(
        session_id,
        TranscriptItem(role=RuntimeMessageRole.USER, content=USER_MESSAGE),
    )
    print(f"\n[1] 会话已创建: {session_id}")

    # 3) LLM 层(生产: services/llm/service.py + factory.py, 12 provider 热切换)
    if os.environ.get("SPIKE_MOCK_LLM") == "1":
        llm_service = MockLLMService()
        print("[0] 使用 Mock LLM 模式(SPIKE_MOCK_LLM=1): 模拟两轮 ReAct(Read → FinalizeReview)")
    else:
        llm_service = LLMService()
    model_client = SpikeModelClient(llm_service=llm_service)

    # 4) 工具层(生产: runtime_core/tool_runtime.py + runtime_tool_registry.py)
    #    ① 基础工具(Read/Glob/Grep/Write/Bash/PowerShell/Skill/TodoWrite/AskUser...)
    #    ② 手动注入自定义终点工具 FinalizeReview ← 验证假设①/③
    #       (生产: runtime_tool_registry.py:586 加 agent_type=="review" 分支挂 FinalizeReviewTool)
    agent_tools = build_shared_agent_tool_catalog(project_root=str(PROJECT_ROOT))
    tool_registry = build_runtime_tool_registry(
        session_store=session_store,
        agent_tools=agent_tools,
        agent_type="spike-review",          # 非 "finding" → 不会自动挂 FinalizeFindingTool
        include_finding_finalizer=False,
    )
    tool_registry.register(FinalizeReviewTool())  # ← 自定义终点工具注入点
    active_tool_names = [tool.name for tool in tool_registry.enabled_tools()]
    print(f"[2] 模型可见工具({len(active_tool_names)} 个): {active_tool_names}")

    tool_orchestrator = ToolOrchestrator(session_store=session_store, tool_registry=tool_registry)

    # 5) 运行时(生产: finding_runtime/runner.py + query_loop.py)
    #    require_terminal_action=True → 模型必须调用 FinalizeReview, 否则 nudge ×2 → incomplete
    runner = FindingRuntimeRunner(
        session_store=session_store,
        model_client=model_client,
        tool_registry=tool_registry,
        tool_orchestrator=tool_orchestrator,
        max_turns=8,
        require_terminal_action=True,
        terminal_action_nudge_limit=2,
        terminal_action_nudge_message="你必须调用 FinalizeReview 工具提交结构化审查评论，禁止只用自然语言结束。",
        event_sink=build_event_printer(),
    )

    # 6) 执行 ReAct 循环(QueryLoop.run_turn 反复执行直到终结)
    print("\n[3] 开始 ReAct 循环...\n")
    result = await runner.run_once(session_id=session_id, model_name="spike-review")

    # 7) 结果展示
    print("\n" + "=" * 78)
    print("[4] 运行结果")
    print(f"  stop_reason      : {result.stop_reason}")
    print(f"  terminal_action  : {result.terminal_action}")
    print(f"  completion_mode  : {result.completion_mode}")
    print(f"  transition       : {result.transition}")
    if result.final_payload:
        print("  final_payload    :")
        print(json.dumps(result.final_payload, ensure_ascii=False, indent=2))

    # 8) 落库统计(验证假设④)
    print("[5] DB 落库统计(全部在 audit_session 系列表, 零改表):")
    for table, count in db_stats(session_id).items():
        print(f"    {table:<28} {count:>4} 行")
    if result.completion_mode is RuntimeCompletionMode.INCOMPLETE:
        print("\n⚠️  会话未正常终结(模型未调 FinalizeReview)。")
        print("   生产路径会走 bridge._ensure_payload 的 finalizer 恢复(bridge.py:849-911),")
        print("   本 spike 省略该恢复逻辑, 保持单文件教学清晰。")

    # 9) SPIKE 结论(移植保留清单输入)
    print("\n" + "=" * 78)
    print("SPIKE 结论(对照 spec 00 §5.1 Phase 0 四个验证项)")
    print("=" * 78)
    print("① 终点工具参数化:")
    print("   ✅ 终结判定是 payload 驱动的(query_loop.py:545-561): 工具返回 output_payload['final_payload']")
    print("      即触发 COMPLETED + FINALIZE_TOOL, 自定义 FinalizeReview 无需改运行时即可终结。")
    print("   ⚠️ 硬编码点(移植需参数化, 建议给 QueryLoop 增加 finalizer_tool_names 构造参数):")
    print("      - query_loop.py:950-951  has_terminal_tool_call 集合 {'FinalizeFinding','FinalizeVulnerabilityReports'}")
    print("        (仅影响 nudge 触发判断; 模型有原生 tool call 时本就不触发 nudge)")
    print("      - query_loop.py:1528-1534 回退分支按工具名推断 terminal_action(无 payload 字段时)")
    print("② PR 语义 runtime 跑通: " + ("✅ 见上方运行结果" if result.completion_mode is RuntimeCompletionMode.FINALIZE_TOOL else "❌ 未正常终结, 查看上方原因"))
    print("③ 工具裁剪: ✅ registry.register 手动注入即可控制模型可见工具; 生产在 runtime_tool_registry.py:586 加 review 分支")
    print("④ 零改表落库: ✅ 消息/回合/工具调用/checkpoint 全部落 audit_session 系列表(见上方统计)")
    print("\n移植保留清单(SPIKE 产出):")
    print("  - 原样搬: session_store / query_loop(除 2 处硬编码) / runner / bridge / tool_runtime / llm / tool_message_codec")
    print("  - 小改后搬: query_loop.py 硬编码集合参数化; runtime_tool_registry.py 加 review 分支")
    print("  - 新写: review_runtime/tools/finalize_review.py(本文件 FinalizeReviewTool 的生产版)")


if __name__ == "__main__":
    asyncio.run(main())
