"""Assemble a fully-wired AgentLoop from config (the composition root).

Phase 06's AgentLoop is dependency-injected; this is where the CLI wires
everything together: settings → LLM client + registry + permission engine +
audit sink + session.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from ..ai import LLMClient
from ..config import GlobalConfig, load_settings, paths
from ..core import Session
from ..engine import AgentLoop, AgentLoopConfig, CompactionConfig, build_context_bundle
from ..engine.tokens import estimate_tokens
from ..hooks import HookJsonlSink, load_hook_manager
from ..permissions import JsonlAuditSink, PermissionEngine
from ..permissions.store import load_permission_rules
from ..skills import SkillRegistry
from ..skills.state import (
    POST_COMPACT_MAX_TOKENS_PER_SKILL,
    POST_COMPACT_SKILLS_TOKEN_BUDGET,
    build_restore_text,
)
from ..tools import ToolRegistry, get_builtin_tools
from ..tools.builtin.skill import SkillTool
from .base_prompt import get_base_prompt
from ..mcp import McpManager, build_mcp_tools
from ..mcp.config import get_all_mcp_configs


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
    mcp_connect_timeout_ms: int = 30_000,  # 阶段 15:每服务器连接超时(失败降级不阻塞)
) -> AgentLoop:
    settings = load_settings(project_dir=cwd)
    client = LLMClient(project_dir=str(cwd), vcr_mode=vcr_mode)
    audit = JsonlAuditSink(paths.config_dir() / "audit.jsonl")
    permissions = PermissionEngine(audit_sink=audit)
    registry = ToolRegistry(get_builtin_tools())
    # 阶段 14:技能系统装配 —— SkillRegistry(用户 + 项目 + 内置)与 SkillTool
    # 一并注入;技能列表作为 ContextBundle 的 availableSkills 段(08 预留扩展位,
    # §9.1,归 fixed 类恒保留);loop 挂 _skills 供 repl 斜杠兜底读取
    skill_registry = SkillRegistry.from_default_paths(cwd)
    registry.register(SkillTool(skill_registry))
    # 阶段 15:MCP 客户端装配 —— 同步预连接全部服务器(每服务器 30s 超时,失败降级
    # 不阻塞启动,spec §7.2 裁决 4),工具即时注入注册表;loop 挂 _mcp 供 /mcp 命令读取
    mcp_manager = build_mcp_manager(cwd, connect_timeout_ms=mcp_connect_timeout_ms)
    _register_mcp_tools(registry, mcp_manager)
    bundle = build_context_bundle(cwd)
    if listing := skill_registry.listing_text(cwd=cwd):
        bundle.sections.append(("availableSkills", listing))
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
    elif session.meta is not None:
        # 12 §8.2/§10.1:恢复已有会话(--continue)时模型指针切换 → 追加
        # model_change entry(装配时注入)。当前模型 = 最后一条 model_change 的
        # `to`(无则回退 meta.model 首行快照)—— 避免每次 --continue 重复追加
        # 污染 model_change 历史(§8.2「切换时追加」语义)。
        current = session.meta.get("model")
        for e in reversed(session._read()[0]):
            if e.type == "model_change":
                current = e.data.get("to")
                break
        if current not in (None, model):
            session.append_model_change(to=model, from_=current)

    loop = AgentLoop(
        AgentLoopConfig(
            client=client,
            tools=registry,
            permissions=permissions,
            request_permission=request_permission,
            system_prompt=system_prompt if system_prompt is not None else get_base_prompt(str(cwd)),
            context_bundle=bundle,  # memoize: once per session (S4);含 availableSkills 段(14 §9.1)
            compaction=CompactionConfig(),  # PI-05 auto-compact (S6)
            model=model,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            cwd=cwd,
            session=session,
            settings=settings,
            history=history,
            hooks=hooks,  # 阶段 09:事件钩子管理器(无配置事件走索引零路径,§4.10.1)
            skill_restore=lambda: _skill_restore(loop),  # 14 §10.2:压缩后技能恢复(闭包晚绑定,压缩时才调用)
        ),
        mode=mode,  # 会话级运行时切换(/mode 命令写实例,不进 config)
    )
    # 阶段 14 §6.1:装配层挂技能注册表(repl 斜杠兜底读取;与 SkillTool 共用)
    loop._skills = skill_registry
    # 阶段 15:挂 MCP 管理器(/mcp 命令读取连接状态;repl 斜杠兜底 prompts)
    loop._mcp = mcp_manager
    return loop


def build_mcp_manager(cwd: Path, connect_timeout_ms: int = 30_000) -> McpManager:
    """构建 McpManager 并同步预连接全部服务器(spec §7.2 裁决 4)。

    读全层配置(含内置托管层),逐服务器连接;失败降级为 failed/needs-auth 不抛,
    保证 MCP 故障不阻塞 CLI 启动。
    """
    from ..mcp import McpManager
    from ..mcp.config import get_all_mcp_configs

    manager = McpManager(configs=get_all_mcp_configs())
    coro = manager.connect_all(timeout_ms=connect_timeout_ms)
    try:
        asyncio.get_running_loop()
        # 已在运行中的事件循环(测试环境):这里不能 await(同步函数),由调用方驱动;
        # connect_all 不启动则工具池为空,不阻塞。测试用注入 manager 方式覆盖连接。
    except RuntimeError:
        asyncio.run(coro)  # 无运行中循环(CLI 入口)时同步跑完
    return manager


def _register_mcp_tools(registry: ToolRegistry, manager: McpManager) -> None:
    """把已连接服务器的 MCP 工具注册进 registry(spec §7.2 装配注入)。

    与 build_mcp_manager 相同的事件循环策略:无运行中循环则同步完成,否则由调用方驱动。
    工具构建失败(如连接未就绪)时静默跳过,不阻塞 CLI 启动。
    """
    async def _build():
        tools = await build_mcp_tools(manager)
        for t in tools:
            registry.register(t)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(_build())
        except Exception:  # noqa: BLE001  # MCP 装配失败不阻塞启动
            pass


def _new_session_id() -> str:
    from datetime import datetime

    # microsecond precision: same-second sessions must not collide
    return datetime.now().strftime("session-%Y%m%d-%H%M%S-%f")


def _skill_restore(loop: AgentLoop) -> str | None:
    """阶段 14 §10.2:压缩后技能恢复段(按 loop._agent_name 隔离)。

    回调在压缩完成处调用,文本并入既有一次性 _recovery_reminder(08/10 机制,
    零新通道)。agent_id=None(主会话)→ 主会话技能记录;子代理 → 隔离键。
    """
    return build_restore_text(agent_id=getattr(loop, "_agent_name", None))


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
