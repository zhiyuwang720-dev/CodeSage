"""Permission engine: the full decision chain (design note #5/#6).

Chain order (mirroring Kode's hasPermissionsToUseTool):
1. normalize mode; 2. system whitelist; 3. bash command analysis (deny/ask);
4. explicit rules (deny > ask > write-protection > allow; Bash content rules
evaluated per sub-command — one denied subcommand denies the compound, only
an all-allowed compound passes); 5. file-tool working-directory constraint →
explicit approval; 6. sensitive reads → explicit approval;
7. needs_permissions() self-declaration → allow; 8. mode post-processing
(plan denies writes, yolo auto-allows what would ask — never
explicit-approval items); 9. audit event.

deny is absolute: no mode may override a deny (yolo only auto-allows "ask").
Write protection is a hard floor: even an explicit allow rule cannot write a
protected path. The working-directory constraint is also absolute: a file
tool targeting a path outside every working_dir asks with explicit approval
even under yolo (Kode's isPathInWorkingDirectories), but explicit allow rules
still win.
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
    """Evaluates tool use against rules + mode, and audits every decision."""

    def __init__(self, audit_sink: AuditSink | None = None):
        self.audit = audit_sink or NullAuditSink()

    def evaluate_tool_use(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        tool: Any = None,  # the Tool object (for needs_permissions), optional
        permissions: dict[str, Any] | None = None,  # settings.permissions
        mode: str | PermissionMode = PermissionMode.DEFAULT,
        cwd: Path | None = None,
        session_permissions: dict[str, Any] | None = None,
        working_dirs: list[Path] | None = None,
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

        # 1. system whitelist — internal harness tools always allowed
        if tool_name in SYSTEM_TOOLS:
            return self._decide(True, "allow", "system", "system tool whitelist", tool_name, tool_input, mode_enum)

        # 2. bash command analysis (deny is absolute; ask needs explicit approval)
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

        # 3. explicit rules: deny > ask > write-protection > allow.
        # Bash content rules (Bash(<cmd>)) are evaluated per sub-command:
        # any denied subcommand denies the compound, only an all-allowed
        # compound passes, mixed (unruled subcommand) falls through to ask.
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

        # 4. write protection is a hard floor, checked before allow rules —
        # even an explicit allow rule cannot write a protected path
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

        # 5. file tools: targets must live inside a working directory — even
        # yolo does not auto-allow out-of-tree access (Kode isPathInWorkingDirectories)
        if tool_name in FILE_TOOLS and target_path is not None and not self._in_working_dirs(target_path, working_dirs):
            return self._decide(
                False, "ask", "working-dir", f"{target_path} is outside the working directories",
                tool_name, tool_input, mode_enum, requires_explicit_approval=True,
            )

        # 6. sensitive reads (keys, .env, credentials) need explicit approval
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

        # 7. self-declared permission-free tools (read-only) — allow
        if tool is not None and not tool.needs_permissions(tool_input):
            return self._decide(True, "allow", "self-declared", "tool declared no permissions needed", tool_name, tool_input, mode_enum)

        # 8. mode post-processing
        if mode_enum == PermissionMode.PLAN and tool_name not in READ_ONLY_TOOLS:
            return self._decide(False, "deny", "plan-mode", f"{tool_name} blocked in plan mode", tool_name, tool_input, mode_enum)
        if tool_name in REQUIRES_EXPLICIT_APPROVAL:
            return self._decide(
                False, "ask", "explicit-approval", f"{tool_name} requires explicit approval", tool_name, tool_input, mode_enum,
                requires_explicit_approval=True,
            )
        if mode_enum == PermissionMode.YOLO:
            return self._decide(True, "allow", "yolo", "auto-allowed by yolo mode", tool_name, tool_input, mode_enum)

        # 9. default: ask (never default-allow unknown tools)
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

    # ---- helpers ----

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
        """Settings rules first, session rules after; within a list, a later
        `!rule` negation cancels earlier matches (Kode gitignore-ish semantics)."""
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
        """[lexical absolute, symlink-expanded real] path candidates, or []
        for non-file tools. Deny/ask/allow rules match either candidate, so
        a deny on /tmp/link/** still holds when /tmp/link → ~/.ssh."""
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
        """Main (symlink-expanded) target path — write protection, working-dir
        and sensitive checks stay on the real path (never bypassed by links)."""
        candidates = PermissionEngine._target_candidates(tool_name, tool_input, cwd)
        return candidates[-1] if candidates else None

    @staticmethod
    def _match_rules(rules: list[Any], tool_name: str, candidates: list[Path]) -> str | None:
        """First matching rule across every path candidate (lexical + real);
        the final None pass keeps bare tool-name rules matching non-file tools."""
        for cand in (*candidates, None):
            hit = match_first(rules, tool_name, cand)
            if hit:
                return hit
        return None

    @staticmethod
    def _summarize(tool_input: dict) -> dict[str, Any]:
        """Audit-safe input summary: paths only, never content/secrets."""
        summary: dict[str, Any] = {}
        for key in ("file_path", "path", "pattern"):
            if key in tool_input:
                summary[key] = str(tool_input[key])[:200]
        return summary
