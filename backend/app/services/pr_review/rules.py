"""确定性规则引擎(阶段 02 §3.6, 来源 evoagent reviewer.py:26-81 MIT)。

在 Review Agent 之前跑第一遍: 确定性规则先兜底, 模型不可用时可独立出审(§7)。
规则可扩充: 向 RULES 追加 (rule_id, severity, category, pattern, title,
description, suggestion, test_hint) 即可; 仅审 diff 新增行。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from app.services.pr_review.diff_lines import parse_added_lines
from app.services.contracts.final_review_contract import ReviewFinding

# evoagent LocalRuleReviewer 原始 6 条 + 扩充; (正则, 严重度, 类别, 标题, 描述, 建议, 测试提示)
_RULE_TUPLE = tuple[
    str, str, str, re.Pattern[str], str, str, str, str
]


@dataclass(frozen=True)
class Rule:
    rule_id: str
    severity: str
    category: str
    pattern: re.Pattern[str]
    title: str
    description: str
    suggestion: str
    test_hint: str


def _rule(
    rule_id: str, severity: str, category: str, pattern: str,
    title: str, description: str, suggestion: str, test_hint: str,
) -> Rule:
    return Rule(rule_id, severity, category, re.compile(pattern), title, description, suggestion, test_hint)


# ── evoagent 原始 6 条(MIT, 文件头注明) ──────────────────────────
BASE_RULES: list[Rule] = [
    _rule(
        "SEC-EVAL", "critical", "security", r"\b(eval|exec)\s*\(",
        "动态代码执行可能导致注入",
        "新增代码调用了动态执行函数；当参数可被外部影响时，攻击者可能执行任意代码。",
        "移除动态执行；使用显式解析器、命令映射表或严格白名单处理输入。",
        "加入恶意表达式与边界输入测试，断言输入不会被当作代码执行。",
    ),
    _rule(
        "SEC-SUBPROCESS-SHELL", "high", "security", r"\bshell\s*=\s*True\b",
        "Shell 调用存在命令注入风险",
        "shell=True 会扩大参数拼接造成命令注入的风险。",
        "使用参数数组并保持 shell=False；对允许值进行白名单验证。",
        "加入包含空格、分号与命令替换字符的输入测试。",
    ),
    _rule(
        "SEC-HARDCODED-SECRET", "high", "security",
        r"(?i)(?<![\w.])\w*(password|passwd|api[_-]?key|secret|token)\w*\s*=\s*['\"][^'\"]{4,}['\"]",
        "疑似硬编码凭据",
        "凭据进入代码仓库后可能通过历史记录、构建日志或制品泄露。",
        "从密钥管理服务或环境变量读取，并立即轮换已经提交的凭据。",
        "测试缺少配置时安全失败，且日志不会输出凭据。",
    ),
    _rule(
        "SEC-SQL-CONCAT", "high", "security", r"(?i)(execute|query)\s*\(\s*(f['\"]|['\"].*(\+|%))",
        "SQL 语句疑似动态拼接",
        "将外部数据拼接到 SQL 中可能产生 SQL 注入。",
        "改用驱动提供的参数化查询与占位符。",
        "加入引号、注释符和布尔表达式等注入载荷测试。",
    ),
    _rule(
        "REL-EMPTY-EXCEPT", "medium", "bug", r"^\s*except\s*(Exception\s*)?:\s*(pass)?\s*$",
        "异常被宽泛捕获",
        "宽泛捕获会隐藏真实故障，使调用方误以为操作成功。",
        "仅捕获可处理的异常，记录必要上下文，并让不可恢复错误向上传播。",
        "加入依赖失败测试，断言错误可观察且不会返回伪成功。",
    ),
    _rule(
        "REL-DEBUG-PRINT", "low", "bug", r"\b(print\s*\(|console\.log\s*\()",
        "新增调试输出",
        "直接输出可能污染服务日志或意外暴露运行数据。",
        "删除调试输出，或改用带级别和脱敏策略的结构化日志。",
        "验证正常请求不会产生包含敏感值的非预期输出。",
    ),
]

# ── 扩充规则(覆盖 benchmark 高发类别: concurrency/data/api/perf/test_gap) ──
EXTRA_RULES: list[Rule] = [
    _rule(
        "CONC-SLEEP-LOCK", "medium", "concurrency", r"time\.sleep\s*\(",
        "循环中疑似用 sleep 等待并发条件",
        "用固定 sleep 等待共享状态会引入竞态与无谓延迟。",
        "使用事件/条件变量或轮询带超时的状态检查。",
        "并发测试断言不存在固定 sleep 依赖。",
    ),
    _rule(
        "DATA-STR-FORMAT-SQL", "medium", "data", r"(?i)\b(select|insert|update|delete)\b.+%s|\.format\(",
        "SQL 语句疑似字符串格式化",
        "字符串格式化构造 SQL 与参数化查询相比更易注入。",
        "改用驱动占位符或 ORM 参数绑定。",
        "加入注入载荷测试。",
    ),
    _rule(
        "API-EXCEPT-SILENT", "medium", "api", r"except\s+\w+\s*:\s*continue\s*$",
        "异常被静默吞掉后继续",
        "捕获后直接 continue 会隐藏失败路径，调用方拿到错误结果。",
        "记录并向上传播，或返回明确的失败语义。",
        "加入错误路径测试断言响应失败。",
    ),
    _rule(
        "PERF-LOOP-QUERY", "medium", "perf", r"for\s+\w+\s+in\s+.*:\s*$",
        "循环体内疑似逐条查询",
        "循环内 N+1 查询/IO 会放大延迟(需结合下一行判断, 规则先标记供复核)。",
        "批量化查询或预取关联数据。",
        "性能基准或查询计数断言。",
    ),
    _rule(
        "TEST-GAP-TODO", "low", "test_gap", r"#\s*TODO\b|#\s*FIXME\b",
        "新增代码带未完成标记",
        "TODO/FIXME 可能意味着边界逻辑未完成或未验证。",
        "完成实现或关联跟踪任务。",
        "确认对应行为有测试覆盖。",
    ),
]

RULES: list[Rule] = BASE_RULES + EXTRA_RULES

# 规则不审的生成物/锁文件后缀(evoagent 同款 + 常见生成目录)
_SKIP_SUFFIXES = (".lock", ".min.js", ".map", ".svg", ".snap")


def _should_skip(path: str) -> bool:
    lowered = path.lower()
    return (
        lowered.endswith(_SKIP_SUFFIXES)
        or "package-lock" in lowered
        or "yarn.lock" in lowered
        or "/migrations/" in lowered
        or lowered.startswith(("dist/", "build/", "vendor/", "node_modules/"))
    )


def run_rules(
    diff_text: str,
    rules: list[Rule] | None = None,
    max_findings: int = 50,
) -> list[ReviewFinding]:
    """确定性规则审查: 仅 diff 新增行, 每行每规则最多一条。"""
    rules = RULES if rules is None else rules
    findings: list[ReviewFinding] = []
    seen: set[tuple[str, str, int]] = set()
    for item in parse_added_lines(diff_text):
        if _should_skip(item.path):
            continue
        for rule in rules:
            if not rule.pattern.search(item.content):
                continue
            key = (rule.rule_id, item.path, item.line)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                ReviewFinding(
                    rule_id=rule.rule_id,
                    severity=rule.severity,  # type: ignore[arg-type]
                    category=rule.category,  # type: ignore[arg-type]
                    title=rule.title,
                    description=f"{rule.description}(第 {item.line} 行: {item.content.strip()[:80]})",
                    file_path=item.path,
                    line_start=item.line,
                    line_end=item.line,
                    code_snippet=item.content,
                    suggestion=rule.suggestion,
                    confidence=0.95,  # 确定性规则, 高置信
                    needs_verification=False,
                    verdict="confirmed",
                    source="rules",
                )
            )
            if len(findings) >= max_findings:
                return findings
    return findings
