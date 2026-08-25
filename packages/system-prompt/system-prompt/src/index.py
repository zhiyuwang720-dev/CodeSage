"""有序系统分节、动态上下文、工具 schema 与提示变量的注册表
(参考实现 system-prompt/index.ts 实现)。

装配面把「贡献注册」与「一次请求的装配」分开:分节/上下文/工具
schema/变量都按作用域链注册,``assemble()`` 每次请求时沿链收集、
排序、插值,再跑 ``system-prompt/assemble`` 瀑布(专家可改写权威
结果,complete 分节在瀑布后被恢复为唯一分节)。装配后文本仍带
``{{variable}}`` 引用,渲染时严格插值:畸形、未知、无值引用抛错,
孤立的 ``{{`` 是字面散文,替换值不再二次扫描。

**Python 实现差异**(均在注释中标出):

- schemastery(z) 的 Config schema → 手写默认与校验(与 session 包
  zod → 手写校验器同策略);
- invariants 服务未实现(批次 3+):装配结果校验(invariant.ts)并入
  assemble() 的返回前同步校验,失败即抛 —— 不等价于 参考实现 的失败
  报告面,但保证同一组不变量不落地。
"""

from __future__ import annotations

import copy
import inspect
import re
from typing import Callable, TypedDict

from cordis import Service

from core.scope import (
    AnonymousEntries,
    NamedEntries,
    ScopedLayers,
    scope_target,
)


class AssembledSection(TypedDict):
    """一次装配的一条分节:贡献名 + 已解析(未插值)文本。"""

    name: str
    text: str


class AssembledContext(TypedDict):
    """一次装配的一条动态上下文贡献。"""

    name: str
    text: str


class AssembleContext(TypedDict):
    """一次提示装配的合并扩展上下文。"""

    scope: object | None
    signal: object | None


class PromptSection(TypedDict):
    """系统提示的一条贡献(注册输入)。"""

    name: str
    order: float
    text: str | Callable
    complete: bool | None


class PromptContext(TypedDict):
    """动态模型上下文的一条贡献。"""

    name: str
    order: float
    text: str | Callable


class PromptAssembly(TypedDict):
    """合并扩展的已装配模型输入:分节与上下文未插值,工具已规范序。"""

    sections: list[AssembledSection]
    contexts: list[AssembledContext]
    tools: list
    variables: dict


class ToolProviderResult(TypedDict):
    """一次装配中一个工具提供者的贡献 + 限制前名字集。"""

    schemas: list
    knownNames: list | None


class Config(TypedDict):
    """插件配置:部署方写的系统提示片段。"""

    includeHarnessIdentity: bool | None
    includeRuntimeContext: bool | None
    persona: str | None
    toolOrder: list | None

__all__ = [
    "AssembledContext",
    "AssembledSection",
    "AssembleContext",
    "Config",
    "PERSONA_ORDER",
    "PERSONA_SECTION",
    "PromptAssembly",
    "PromptContext",
    "PromptSection",
    "SystemPrompt",
    "TOOL_ORDER_REST",
    "ToolProviderResult",
    "join_context_sections",
    "render_context_sections",
    "render_context_snapshot",
    "render_prompt",
]

#: 部署人格的分节名与顺序:组合可以替换这个槽(agent preset 用自己
#: 的分节影子部署人格),两侧同名正是替换生效而非重复的机制。
PERSONA_SECTION = "deployment:persona"
#: 人格槽的提示顺序;模型读到的第一个分节。
PERSONA_ORDER = 0
#: 未列入 toolOrder 的工具的占位名(配置侧保留标记)。
TOOL_ORDER_REST = "<unlisted-tools>"

#: 合法变量名:花括号之间怎么写。
_VARIABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
#: 扫描位置上的完整 ``{{...}}`` 引用组(校验在组外做)。
_GROUP_AT = re.compile(r"^\{\{([^{}]*)\}\}")


def validate_tool_order(tool_order: list | None) -> list | None:
    """校验重复名与必需的 TOOL_ORDER_REST 标记。

    已注册名在装配时校验(插件尚未加载,这里查不到)。
    """
    if tool_order is None:
        return None
    seen = set()
    for name in tool_order:
        if name in seen:
            raise RuntimeError(f'toolOrder lists "{name}" more than once')
        seen.add(name)
    if TOOL_ORDER_REST not in seen:
        raise RuntimeError(
            f'toolOrder must contain the "{TOOL_ORDER_REST}" rest entry (where unlisted tools are inserted)'
        )
    return tool_order


def _compare_tool_names(a: dict, b: dict) -> int:
    """词典序(码元序)名称比较 —— 与 locale 无关,每台机器序一致。"""
    return -1 if a["name"] < b["name"] else 1 if a["name"] > b["name"] else 0


def order_tools(tools: list, tool_order: list | None, known_names: set) -> list:
    """应用配置的工具顺序,未列出工具按词典序插入 TOOL_ORDER_REST。

    未知配置名失败;已知但被作用域隐藏的名字可以缺席。
    """
    if any(tool["name"] == TOOL_ORDER_REST for tool in tools):
        raise RuntimeError(
            f'tool provider returned reserved tool name "{TOOL_ORDER_REST}" '
            "(reserved for toolOrder's rest entry)"
        )
    if tool_order is None:
        return sorted(tools, key=lambda tool: tool["name"])
    unknown = [name for name in tool_order if name != TOOL_ORDER_REST and name not in known_names]
    if unknown:
        suffix = "s" if len(unknown) > 1 else ""
        raise RuntimeError(
            f"toolOrder lists unregistered tool{suffix} {', '.join(f'\"{n}\"' for n in unknown)}; "
            f"known tools: {', '.join(sorted(known_names)) or '(none)'}"
        )
    listed = set(tool_order)
    rest = sorted((tool for tool in tools if tool["name"] not in listed), key=lambda tool: tool["name"])
    by_name = {tool["name"]: tool for tool in tools}
    ordered: list = []
    for name in tool_order:
        ordered.extend([by_name[name]] if name != TOOL_ORDER_REST else rest)
    return ordered


def _interpolate(input_, variables: dict, kind: str) -> str:
    """插值一个分节或上下文,把诊断归因到它的属主输入。"""
    text = input_["text"]
    result: list[str] = []
    last = 0
    open_at = text.find("{{")
    while open_at >= 0:
        group = _GROUP_AT.match(text[open_at:])
        if group is None:
            # 后面还有闭花括号就是畸形引用;否则是字面散文。
            if text.find("}}", open_at + 2) >= 0:
                raise RuntimeError(
                    f'malformed prompt variable reference at "{text[open_at:open_at + 16]}…" '
                    f'in {kind} "{input_["name"]}" (references are complete simple {{name}} groups)'
                )
            result.append(text[last:open_at + 2])
            last = open_at + 2
            open_at = text.find("{{", last)
            continue
        name = group.group(0)[2:-2]
        if _VARIABLE_NAME.match(name) is None:
            raise RuntimeError(
                f'malformed prompt variable reference "{{{{{name}}}}}" in {kind} "{input_["name"]}" '
                f"(variable names match {_VARIABLE_NAME.pattern})"
            )
        if name not in variables:
            known = ", ".join(variables) or "(none)"
            raise RuntimeError(
                f'unknown prompt variable "{{{{{name}}}}}" in {kind} "{input_["name"]}"; '
                f"registered variables: {known}"
            )
        value = variables[name]
        if value is None:
            raise RuntimeError(
                f'prompt variable "{{{{{name}}}}}" has no value for this assembly '
                f'({kind} "{input_["name"]}")'
            )
        result.append(text[last:open_at] + value)
        last = open_at + len(group.group(0))
        open_at = text.find("{{", last)
    result.append(text[last:])
    return "".join(result)


def render_prompt(assembly: dict) -> str:
    """严格插值 ``{{variable}}``、丢弃空分节、空行连接剩余部分。"""
    return "\n\n".join(
        section_text
        for section in assembly["sections"]
        if (section_text := _interpolate(section, assembly["variables"], "section")) != ""
    )


def join_context_sections(sections) -> str:
    """把已渲染的分节列表连成模型的快照文本;无正文时返回 ''。"""
    body = "\n\n".join(section["text"] for section in sections)
    if body == "":
        return ""
    return f"Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n{body}"


def render_context_sections(assembly: dict) -> list:
    """保留贡献名的同一快照:每个渲染到非空文本的上下文一条。"""
    rendered = []
    for context in assembly["contexts"]:
        text = _interpolate(context, assembly["variables"], "context")
        if text != "":
            rendered.append({"name": context["name"], "text": text})
    return rendered


def render_context_snapshot(assembly: dict) -> str:
    """渲染完整动态上下文快照;无活动上下文时返回 ''。"""
    return join_context_sections(render_context_sections(assembly))


def validate_assembly(assembly: dict) -> None:
    """装配结果不变式(invariant.ts):重复名/空名/非字符串文本/非法变量名。"""
    section_names = set()
    for section in assembly["sections"]:
        if section["name"] == "":
            raise RuntimeError("assembled section names must be non-empty")
        if section["name"] in section_names:
            raise RuntimeError(f'assembled section name "{section["name"]}" is duplicated')
        section_names.add(section["name"])
        if not isinstance(section["text"], str):
            raise RuntimeError(f'assembled section "{section["name"]}" text must be a string')
    context_names = set()
    for context in assembly["contexts"]:
        if context["name"] == "":
            raise RuntimeError("assembled context names must be non-empty")
        if context["name"] in context_names:
            raise RuntimeError(f'assembled context name "{context["name"]}" is duplicated')
        context_names.add(context["name"])
        if not isinstance(context["text"], str):
            raise RuntimeError(f'assembled context "{context["name"]}" text must be a string')
    for tool in assembly["tools"]:
        if tool["name"] == "":
            raise RuntimeError("assembled tool names must be non-empty")
    for name, value in assembly["variables"].items():
        if _VARIABLE_NAME.match(name) is None:
            raise RuntimeError(f'assembled variable name "{name}" is invalid')
        if value is not None and not isinstance(value, str):
            raise RuntimeError(f'assembled variable "{name}" must be a string or undefined')


class PromptLayer:
    """一个全局或作用域层拥有的全部提示注册。"""

    def __init__(self, scope) -> None:
        #: 参考实现 对全局/作用域重复给出不同提示(per-agent 覆盖走 agent.ctx);
        #: 命名空间差异保留,文案差异不承重,省略。
        def duplicate(kind: str) -> Callable:
            def raise_duplicate(name: str):
                raise RuntimeError(f'prompt {kind} "{name}" is already registered')

            return raise_duplicate

        self.sections = NamedEntries(duplicate("section"))
        self.contexts = NamedEntries(duplicate("context"))
        self.runtime_context_suppressors = AnonymousEntries()
        self.tool_providers = AnonymousEntries()
        self.variables = NamedEntries(duplicate("variable"))

    def is_empty(self) -> bool:
        return (
            self.sections.is_empty()
            and self.contexts.is_empty()
            and self.runtime_context_suppressors.is_empty()
            and self.tool_providers.is_empty()
            and self.variables.is_empty()
        )


class SystemPrompt(Service):
    """每次模型步骤前装配的提示输入注册服务(ctx.systemPrompt)。"""

    def __init__(self, ctx, config: dict | None = None) -> None:
        super().__init__(ctx, "systemPrompt")
        config = config or {}
        self.tool_order = validate_tool_order(config.get("toolOrder"))
        #: 全局层 + 各作用域层;任何注册/注销都通知 system-prompt/change。
        self.layers = ScopedLayers(
            PromptLayer,
            lambda: self.ctx.emit("system-prompt/change"),
        )
        #: 保持 harness 属主的开篇与所选 loop 插件无关。
        if config.get("includeHarnessIdentity", True):
            self.section({
                "name": "harness:identity",
                "order": -100,
                "text": "You are an AI agent powered by DeepSeek Harness.",
            })
        self.section({
            "name": PERSONA_SECTION,
            "order": PERSONA_ORDER,
            "text": config.get("persona", ""),
        })
        if not config.get("includeRuntimeContext", True):
            self.suppress_runtime_context()

    def section(self, section: dict) -> Callable:
        """在调用方上下文的作用域注册一个有序提示分节。

        作用域分节影子同名全局分节;同层重复名与非有限顺序抛错。
        """
        if not _is_finite(section.get("order")):
            raise TypeError(f'prompt section "{section.get("name")}" order must be a finite number')
        return self.layers.effect(
            self.ctx,
            lambda layer: layer.sections.insert(section["name"], section),
            "systemPrompt.section()",
        )

    def context(self, context: dict) -> Callable:
        """在调用方上下文的作用域注册有序动态上下文。"""
        if not _is_finite(context.get("order")):
            raise TypeError(f'prompt context "{context.get("name")}" order must be a finite number')
        return self.layers.effect(
            self.ctx,
            lambda layer: layer.contexts.insert(context["name"], context),
            "systemPrompt.context()",
        )

    def suppress_runtime_context(self) -> Callable:
        """在不改动属主服务的前提下抑制调用方作用域的全部动态上下文。"""
        return self.layers.effect(
            self.ctx,
            lambda layer: layer.runtime_context_suppressors.append(True),
            "systemPrompt.suppressRuntimeContext()",
        )

    def tools(self, provider: Callable) -> Callable:
        """在调用方作用域注册一个工具 schema 提供者,每次装配求值。"""
        return self.layers.effect(
            self.ctx,
            lambda layer: layer.tool_providers.append(provider),
            "systemPrompt.tools()",
        )

    def variable(self, name: str, provider: Callable) -> Callable:
        """在调用方作用域注册一个提示变量;作用域值影子全局值。"""
        if _VARIABLE_NAME.match(name) is None:
            raise RuntimeError(
                f'invalid prompt variable name "{name}" (must match {_VARIABLE_NAME.pattern})'
            )
        return self.layers.effect(
            self.ctx,
            lambda layer: layer.variables.insert(name, provider),
            "systemPrompt.variable()",
        )

    async def assemble(self, context: dict | None = None) -> dict:
        """收集全局与作用域提供者、分离工具参数、应用规范顺序,再跑瀑布。

        作用域分节与变量影子全局;返回的瀑布值是权威的,除了生效的
        complete 分节在之后被恢复为唯一分节。
        """
        context = context or {}
        scope = context.get("scope")
        scope_layers = self.layers.chain_layers(scope)
        runtime_suppressed = (
            not self.layers.global_.runtime_context_suppressors.is_empty()
            or any(not layer.runtime_context_suppressors.is_empty() for layer in scope_layers)
        )
        # 作用域变量影子全局:全局先写,链上最远祖先先写,最近作用域最后赢。
        variables = {name: provider(context) for name, provider in self.layers.global_.variables.entries()}
        for layer in scope_layers:
            for name, provider in layer.variables.entries():
                variables[name] = provider(context)
        section_by_name = self.layers.merge(scope, lambda layer: layer.sections)
        context_by_name = self.layers.merge(scope, lambda layer: layer.contexts)
        # 对照限制前名字集校验顺序,同时收集可见 schema。
        providers = [
            *self.layers.global_.tool_providers.values(),
            *[provider for layer in scope_layers for provider in layer.tool_providers.values()],
        ]
        collected: list = []
        known_names = set()
        for provider in providers:
            result = provider(context)
            schemas = [
                {"name": s["name"], "description": s["description"], "parameters": copy.deepcopy(s["parameters"])}
                for s in result["schemas"]
            ]
            accepted = result.get("knownNames") or [tool["name"] for tool in schemas]
            collected.extend(schemas)
            known_names.update(accepted)
        section_definitions = sorted(section_by_name.values(), key=lambda s: s["order"])
        complete_sections = [s for s in section_definitions if s.get("complete") is True]
        if len(complete_sections) > 1:
            raise RuntimeError(
                "multiple complete prompt sections are active: "
                + ", ".join(f'"{s["name"]}"' for s in complete_sections)
            )
        complete_section = None
        sections = []
        for section in section_definitions:
            assembled = {
                "name": section["name"],
                "text": section["text"](context) if callable(section["text"]) else section["text"],
            }
            if section.get("complete") is True:
                complete_section = dict(assembled)
            sections.append(assembled)
        assembly = {
            "sections": sections,
            "contexts": (
                []
                if runtime_suppressed
                else [
                    {
                        "name": entry["name"],
                        "text": entry["text"](context) if callable(entry["text"]) else entry["text"],
                    }
                    for entry in sorted(context_by_name.values(), key=lambda c: c["order"])
                ]
            ),
            "tools": order_tools(collected, self.tool_order, known_names),
            "variables": variables,
        }
        #: 参考实现经 ctx.waterfall(scopeTarget(this, scope), ...) 派发;
        #: 作用域过滤的监听者只收到自己作用域的装配。无监听者时
        #: cordis 同步返回内层值,有 async 监听者时返回协程。
        result = self.ctx.waterfall(
            scope_target(self, scope),
            "system-prompt/assemble",
            assembly,
            context,
            lambda: assembly,
        )
        transformed = await result if inspect.isawaitable(result) else result
        if complete_section is None and not runtime_suppressed:
            validate_assembly(transformed)
            return transformed
        final = {
            **transformed,
            "sections": transformed["sections"] if complete_section is None else [complete_section],
            "contexts": [] if runtime_suppressed else transformed["contexts"],
        }
        validate_assembly(final)
        return final


def _is_finite(value) -> bool:
    """JS Number.isFinite 语义:数字且有限。"""
    return isinstance(value, (int, float)) and value == value and abs(value) != float("inf")
