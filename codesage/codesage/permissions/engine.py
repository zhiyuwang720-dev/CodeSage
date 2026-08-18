"""权限引擎:完整决策链(设计说明 #5/#6)。

链序(镜像 Kode 的 hasPermissionsToUseTool):
1. 归一化 mode;2. 系统白名单;3. Bash 命令分析(deny/ask);
4. 显式规则(deny > ask > 写保护 > allow;Bash 内容规则按子命令求值——
一个被拒子命令拒绝整条复合命令,只有全 allow 的复合命令通过);5. 文件工具
工作目录约束 → 显式批准;6. 敏感读取 → 显式批准;
7. needs_permissions() 自声明 → allow;8. mode 后处理(plan 拒写,
yolo 自动放行本会 ask 的项——显式批准项除外);9. 审计事件。

deny 是绝对的:任何 mode 都不能覆盖 deny(yolo 只自动放行 "ask")。
写保护是硬地板:即使显式 allow 规则也不能写保护路径。工作目录约束同样
绝对:目标在工作目录外的文件工具即使 yolo 也要求显式批准(Kode 的
isPathInWorkingDirectories),但显式 allow 规则仍然优先。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import AuditSink, NullAuditSink, ToolAuditEvent
from .bash_rules import analyze_bash_command, rm_protected_targets
from .modes import (
    READ_ONLY_TOOLS,
    REQUIRES_EXPLICIT_APPROVAL,
    SYSTEM_TOOLS,
    PermissionMode,
    normalize_mode,
)
from .paths import is_sensitive_path, is_write_protected, resolve_candidates
from .rules import FILE_TOOLS, bash_rules_match, extract_rules, match_first


@dataclass(slots=True)
class PermissionDecision:
    allowed: bool
    mode: str = "ask"  # allow | ask | deny
    reason: str | None = None
    source: str | None = None
    requires_explicit_approval: bool = False


class PermissionEngine:
    """按规则 + mode 评估工具使用,并对每次决策做审计。"""

    def __init__(self, audit_sink: AuditSink | None = None):
        self.audit = audit_sink or NullAuditSink()

    def evaluate_tool_use(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        tool: Any = None,  # Tool 对象(needs_permissions 用),可选
        permissions: dict[str, Any] | None = None,  # settings.permissions
        mode: str | PermissionMode = PermissionMode.DEFAULT,
        cwd: Path | None = None,
        session_permissions: dict[str, Any] | None = None,
        working_dirs: list[Path] | None = None,
        skill_allowed_tools: frozenset[str] | None = None,  # 阶段 14 §7.1:技能授权(可选,默认 None = 决策链零变化)
    ) -> PermissionDecision:
        mode_enum = normalize_mode(mode)
        tool_input = tool_input or {}
        cwd = cwd or Path.cwd()
        if working_dirs is None:
            working_dirs = [cwd]
        try:
            working_dirs = [wd.resolve() for wd in working_dirs]
        except OSError:
            working_dirs = [cwd.resolve()]
        merged = self._merge_rules(permissions, session_permissions)
        candidates = self._target_candidates(tool_name, tool_input, cwd)
        target_path = candidates[-1] if candidates else None

        # 1. 系统白名单 —— 内部 harness 工具恒允许
        if tool_name in SYSTEM_TOOLS:
            return self._decide(True, "allow", "system", "system tool whitelist", tool_name, tool_input, mode_enum)

        # 2. Bash 命令分析(deny 绝对;ask 需显式批准)
        if tool_name == "Bash":
            analysis = analyze_bash_command(
                str(tool_input.get("command") or ""), working_dirs=working_dirs, cwd=cwd
            )
            if analysis.decision == "deny":
                return self._decide(False, "deny", "bash-rules", analysis.reason, tool_name, tool_input, mode_enum)
            if analysis.decision == "ask":
                return self._decide(
                    False, "ask", "bash-rules", analysis.reason, tool_name, tool_input, mode_enum,
                    requires_explicit_approval=True,
                )

        # 3. 显式规则:deny > ask > 写保护 > allow。
        # Bash 内容规则(Bash(<cmd>))按子命令求值:任一被拒子命令拒绝整条复合,
        # 全 allow 的复合才通过,混合(未定规则子命令)落入 ask。
        command_text = str(tool_input.get("command") or "")
        denied = self._match_rules(merged["deny"], tool_name, candidates)
        if not denied and tool_name == "Bash":
            denied = bash_rules_match(merged["deny"], command_text, require_all=False)
        if denied:
            return self._decide(False, "deny", denied, f"denied by rule: {denied}", tool_name, tool_input, mode_enum)
        asked = self._match_rules(merged["ask"], tool_name, candidates)
        if not asked and tool_name == "Bash":
            asked = bash_rules_match(merged["ask"], command_text, require_all=False)
        if asked:
            return self._decide(False, "ask", asked, f"asked by rule: {asked}", tool_name, tool_input, mode_enum)

        # 4. 写保护是硬地板,先于 allow 规则检查 ——
        # 即使显式 allow 规则也不能写保护路径
        if tool_name in FILE_TOOLS and target_path is not None and is_write_protected(target_path):
            return self._decide(
                False, "ask", "write-protection", f"{target_path} is write-protected", tool_name, tool_input, mode_enum,
                requires_explicit_approval=True,
            )

        allowed = self._match_rules(merged["allow"], tool_name, candidates)
        if not allowed and tool_name == "Bash":
            allowed = bash_rules_match(merged["allow"], command_text, require_all=True)
        if allowed:
            return self._decide(True, "allow", allowed, f"allowed by rule: {allowed}", tool_name, tool_input, mode_enum)

        # 5. 文件工具:目标必须位于工作目录内 —— 即使 yolo 也不自动放行
        # 目录外访问(Kode isPathInWorkingDirectories)
        if tool_name in FILE_TOOLS and target_path is not None and not self._in_working_dirs(target_path, working_dirs):
            return self._decide(
                False, "ask", "working-dir", f"{target_path} is outside the working directories",
                tool_name, tool_input, mode_enum, requires_explicit_approval=True,
            )

        # 6. 敏感读取(密钥、.env、凭据)需显式批准
        if (
            tool_name in FILE_TOOLS
            and target_path is not None
            and is_sensitive_path(target_path)
            and not (mode_enum == PermissionMode.PLAN and tool_name in READ_ONLY_TOOLS)
        ):
            return self._decide(
                False, "ask", "sensitive-path", f"{target_path} is sensitive", tool_name, tool_input, mode_enum,
                requires_explicit_approval=True,
            )

        # 7. 自声明免权限工具(只读)— allow
        if tool is not None and not tool.needs_permissions(tool_input):
            return self._decide(True, "allow", "self-declared", "tool declared no permissions needed", tool_name, tool_input, mode_enum)

        # 8. mode 后处理
        if mode_enum == PermissionMode.PLAN and tool_name not in READ_ONLY_TOOLS:
            return self._decide(False, "deny", "plan-mode", f"{tool_name} blocked in plan mode", tool_name, tool_input, mode_enum)
        if tool_name in REQUIRES_EXPLICIT_APPROVAL:
            return self._decide(
                False, "ask", "explicit-approval", f"{tool_name} requires explicit approval", tool_name, tool_input, mode_enum,
                requires_explicit_approval=True,
            )
        if mode_enum == PermissionMode.YOLO:
            return self._decide(True, "allow", "yolo", "auto-allowed by yolo mode", tool_name, tool_input, mode_enum)

        # 8.5 技能授权(阶段 14 §7.1):最弱授权 —— 只豁免「无规则无地板时的
        # 默认 ask」。走到此处说明 deny/ask 规则、写保护、工作目录、敏感路径、
        # 显式批准、plan 模式、yolo 都已在前返回(授权不绕过任何硬地板);
        # Bash 的 REQUIRES_EXPLICIT_APPROVAL 在第 8 步已 ask,授权不豁免。
        if skill_allowed_tools and tool_name in skill_allowed_tools:
            return self._decide(
                True, "allow", "skill-allowed-tools",
                "granted by skill allowed-tools", tool_name, tool_input, mode_enum,
            )

        # 9. 默认:ask(未知工具绝不默认放行)
        return self._decide(False, "ask", "default", f"no rule for {tool_name}", tool_name, tool_input, mode_enum)

    def floor_check(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        cwd: Path | None = None,
        mode: str | PermissionMode = PermissionMode.DEFAULT,
    ) -> PermissionDecision | None:
        """写保护地板(阶段 09 §5.3):仅含第 4 步逻辑(写保护路径)。

        钩子 allow 不得突破写保护(本类第 4 步是硬地板):命中返回
        requires_explicit_approval=True 的 ask 决策,由 loop 侧按既有 ask 流程
        (request_permission)人工确认;审计经 _decide 既有路径(source=write-protection,
        §8.1 的 floor 降级第二条事件)。未命中返回 None。决策链本方法外一行不改。

        Bash 分支(与 FILE_TOOLS 同等地板):analyze_bash_command 的 deny 判定
        (rm/rmdir 保护路径,如 `rm -rf ~`)不得被钩子 allow 绕过;rm/rmdir 目标
        命中写保护组件(`rm -rf .git` 类,rm_protected_targets)同款降级。
        """
        tool_input = tool_input or {}
        cwd = cwd or Path.cwd()
        if tool_name == "Bash":
            command = str(tool_input.get("command") or "")
            analysis = analyze_bash_command(command, working_dirs=[cwd], cwd=cwd)
            if analysis.decision == "deny":
                return self._decide(
                    False, "ask", "write-protection", f"bash: {analysis.reason}",
                    tool_name, tool_input, normalize_mode(mode), requires_explicit_approval=True,
                )
            hits = rm_protected_targets(command, cwd)
            if hits:
                return self._decide(
                    False, "ask", "write-protection",
                    f"bash writes protected path(s): {', '.join(hits)}",
                    tool_name, tool_input, normalize_mode(mode), requires_explicit_approval=True,
                )
            return None
        target_path = self._target_path(tool_name, tool_input, cwd)
        if tool_name in FILE_TOOLS and target_path is not None and is_write_protected(target_path):
            return self._decide(
                False, "ask", "write-protection", f"{target_path} is write-protected",
                tool_name, tool_input, normalize_mode(mode), requires_explicit_approval=True,
            )
        return None

    # ---- 辅助 ----

    def _decide(
        self,
        allowed: bool,
        mode: str,
        source: str,
        reason: str,
        tool_name: str,
        tool_input: dict[str, Any],
        mode_enum: PermissionMode,
        *,
        requires_explicit_approval: bool = False,
    ) -> PermissionDecision:
        decision = PermissionDecision(
            allowed=allowed,
            mode=mode,
            reason=reason,
            source=source,
            requires_explicit_approval=requires_explicit_approval,
        )
        self.audit.emit(
            ToolAuditEvent(
                tool_name=tool_name,
                decision=decision.mode,
                reason=reason,
                source=source,
                mode=mode_enum.value,
                input_summary=self._summarize(tool_input),
            )
        )
        return decision

    @staticmethod
    def _merge_rules(permissions: dict | None, session: dict | None) -> dict[str, list]:
        """settings 规则在前、会话规则在后;同一列表内,靠后的 `!rule` 取反
        抵消先前匹配(Kode gitignore 式语义)。"""
        merged: dict[str, list] = {"allow": [], "deny": [], "ask": []}
        for source in (permissions, session):
            for key, values in extract_rules(source).items():
                merged[key].extend(values)
        return merged

    @staticmethod
    def _in_working_dirs(target: Path, working_dirs: list[Path]) -> bool:
        return any(target.is_relative_to(wd) for wd in working_dirs)

    @staticmethod
    def _target_candidates(tool_name: str, tool_input: dict, cwd: Path) -> list[Path]:
        """[词法绝对、symlink 展开后真实] 路径候选;非文件工具返回 []。
        deny/ask/allow 规则匹配任一候选,故 /tmp/link/** 的 deny 在
        /tmp/link → ~/.ssh 时仍然生效。"""
        if tool_name not in FILE_TOOLS:
            return []
        raw = tool_input.get("file_path") or tool_input.get("path")
        if not raw:
            return []
        p = Path(str(raw))
        if not p.is_absolute():
            p = cwd / p
        return resolve_candidates(p)

    @staticmethod
    def _target_path(tool_name: str, tool_input: dict, cwd: Path) -> Path | None:
        """主(symlink 展开后)目标路径 —— 写保护、工作目录与敏感检查都
        作用在真实路径上(永不因链接被绕过)。"""
        candidates = PermissionEngine._target_candidates(tool_name, tool_input, cwd)
        return candidates[-1] if candidates else None

    @staticmethod
    def _match_rules(rules: list[Any], tool_name: str, candidates: list[Path]) -> str | None:
        """遍历每个路径候选(词法 + 真实)返回首个匹配规则;
        最后的 None 轮保持裸工具名规则可匹配非文件工具。"""
        for cand in (*candidates, None):
            hit = match_first(rules, tool_name, cand)
            if hit:
                return hit
        return None

    @staticmethod
    def _summarize(tool_input: dict) -> dict[str, Any]:
        """审计安全输入摘要:仅路径类字段,绝不落内容/密钥。"""
        summary: dict[str, Any] = {}
        for key in ("file_path", "path", "pattern"):
            if key in tool_input:
                summary[key] = str(tool_input[key])[:200]
        return summary
