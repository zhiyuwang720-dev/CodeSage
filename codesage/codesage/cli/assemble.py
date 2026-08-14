"""Assemble a fully-wired AgentLoop from config (the composition root).

Phase 06's AgentLoop is dependency-injected; this is where the CLI wires
everything together: settings → LLM client + registry + permission engine +
audit sink + session.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..ai import LLMClient
from ..config import GlobalConfig, load_settings, paths
from ..core import Session
from ..engine import AgentLoop, AgentLoopConfig, CompactionConfig, build_context_bundle
from ..hooks import HookJsonlSink, load_hook_manager
from ..permissions import JsonlAuditSink, PermissionEngine
from ..permissions.store import load_permission_rules
from ..tools import ToolRegistry, get_builtin_tools
from .base_prompt import get_base_prompt


def session_root() -> Path:
    from ..config import paths

    return paths.config_dir() / "sessions"


def build_loop(
    *,
    cwd: Path,
    mode: str = "default",
    model: str = "main",
    max_turns: int = 100,
    max_budget_usd: float | None = None,
    request_permission=None,
    vcr_mode: str | None = None,
    session_id: str | None = None,
    system_prompt: str | None = None,
    project_key: str | None = None,
    session: Session | None = None,  # existing session (--continue); else new
    history: list | None = None,  # prior turns as context (--continue)
) -> AgentLoop:
    settings = load_settings(project_dir=cwd)
    client = LLMClient(project_dir=str(cwd), vcr_mode=vcr_mode)
    audit = JsonlAuditSink(paths.config_dir() / "audit.jsonl")
    permissions = PermissionEngine(audit_sink=audit)
    registry = ToolRegistry(get_builtin_tools())
    # 阶段 09:事件钩子 —— 快照语义(§3.2:此处解析一次,会话中 settings.json 修改
    # 不生效);hooks.jsonl = 执行流审计(§8.1);http_hook_urls 为 settings 顶层
    # 白名单字段(extra=allow,缺省 None = 全禁,§4.9)
    hooks = load_hook_manager(
        settings.hooks,
        client=client,
        audit=audit,
        hooks_sink=HookJsonlSink(paths.config_dir() / "hooks.jsonl"),
        http_hook_urls=getattr(settings, "http_hook_urls", None),
        registry=registry,
    )
    if session is None:
        session = Session(session_id or _new_session_id(), session_root(), project_key=project_key)
        # 12 §8.1:新建会话首行写 meta entry(会话自描述锚点;show_thinking
        # 缺省 False,system_prompt_hash = sha256 截断,pointer 名不解析字面量)
        resolved_prompt = system_prompt if system_prompt is not None else get_base_prompt(str(cwd))
        session.append_meta(
            model=model,
            show_thinking=False,
            cwd=str(cwd),
            system_prompt_hash=hashlib.sha256(resolved_prompt.encode("utf-8")).hexdigest()[:12],
            session_id=session.session_id,
        )
    elif (prev := session.meta) is not None and prev.get("model") not in (None, model):
        # 12 §8.2/§10.1:恢复已有会话(--continue)时模型指针不同 → 追加
        # model_change entry(装配时注入),审计/恢复不用猜当时配置
        session.append_model_change(to=model, from_=prev.get("model"))

    return AgentLoop(
        AgentLoopConfig(
            client=client,
            tools=registry,
            permissions=permissions,
            request_permission=request_permission,
            system_prompt=system_prompt if system_prompt is not None else get_base_prompt(str(cwd)),
            context_bundle=build_context_bundle(cwd),  # memoize: once per session (S4)
            compaction=CompactionConfig(),  # PI-05 auto-compact (S6)
            model=model,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            cwd=cwd,
            session=session,
            settings=settings,
            history=history,
            hooks=hooks,  # 阶段 09:事件钩子管理器(无配置事件走索引零路径,§4.10.1)
        ),
        mode=mode,  # 会话级运行时切换(/mode 命令写实例,不进 config)
    )


def _new_session_id() -> str:
    from datetime import datetime

    # microsecond precision: same-second sessions must not collide
    return datetime.now().strftime("session-%Y%m%d-%H%M%S-%f")


def apply_tool_filter(loop: AgentLoop, allowed: str | None, disallowed: str | None) -> None:
    """Restrict the loop's registry: keep only --allowedTools, drop --disallowedTools (comma-separated).

    The model sees only the surviving specs, so a removed tool reads as
    "Unknown tool" — the precise surface-control path for unattended runs.
    """
    if not allowed and not disallowed:
        return
    tools = loop.tools.all()
    if allowed:
        keep = {name.strip() for name in allowed.split(",") if name.strip()}
        tools = [t for t in tools if t.name in keep]
    if disallowed:
        drop = {name.strip() for name in disallowed.split(",") if name.strip()}
        tools = [t for t in tools if t.name not in drop]
    loop.tools = ToolRegistry(tools)
