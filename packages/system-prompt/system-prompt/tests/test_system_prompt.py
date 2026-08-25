"""提示装配注册表单测:渲染/插值/工具排序/注册与作用域影子/装配瀑布。"""

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]  # 仓库根
_PACKAGES = Path(__file__).resolve().parents[3]  # packages/
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))
# 连字符包目录无法在 import 语句里拼出:经 importlib 加载后注册别名
# (伞包键也注册:import system_prompt 时先命中 sys.modules)
_pkg = importlib.import_module("system-prompt.system-prompt")
sys.modules["system_prompt"] = _pkg
sys.modules["system_prompt.system_prompt"] = _pkg

from cordis import Context  # noqa: E402

from system_prompt.system_prompt.src.index import (  # noqa: E402
    PERSONA_ORDER,
    PERSONA_SECTION,
    TOOL_ORDER_REST,
    SystemPrompt,
    join_context_sections,
    order_tools,
    render_context_sections,
    render_context_snapshot,
    render_prompt,
    validate_tool_order,
)

from core.scope import create_scope, scope_of  # noqa: E402


def _assembly(sections=(), contexts=(), tools=(), variables=None):
    return {"sections": list(sections), "contexts": list(contexts), "tools": list(tools), "variables": variables or {}}


# ---- 渲染面 ----


def test_render_prompt_interpolates_and_drops_empty():
    assembly = _assembly(
        sections=[{"name": "a", "text": "hi {{who}}"}, {"name": "b", "text": ""}],
        variables={"who": "world"},
    )
    assert render_prompt(assembly) == "hi world"


def test_render_prompt_literal_lone_brace():
    """孤立的 {{ 无后续 }} 是字面散文,不抛。"""
    assembly = _assembly(sections=[{"name": "a", "text": "cost {{ plus more"}])
    assert render_prompt(assembly) == "cost {{ plus more"


def test_render_prompt_malformed_reference_throws():
    # 完整组但名字非法(含空格):malformed 抛错
    assembly = _assembly(sections=[{"name": "a", "text": "text {{bad name}} tail"}])
    with pytest.raises(RuntimeError, match="malformed"):
        render_prompt(assembly)


def test_render_prompt_unknown_variable_throws():
    assembly = _assembly(sections=[{"name": "a", "text": "{{nope}}"}], variables={"known": "x"})
    with pytest.raises(RuntimeError, match="unknown prompt variable"):
        render_prompt(assembly)


def test_render_prompt_undefined_value_throws():
    assembly = _assembly(sections=[{"name": "a", "text": "{{who}}"}], variables={"who": None})
    with pytest.raises(RuntimeError, match="has no value"):
        render_prompt(assembly)


def test_render_prompt_invalid_name_throws():
    assembly = _assembly(sections=[{"name": "a", "text": "{{1bad}}"}])
    with pytest.raises(RuntimeError, match="malformed prompt variable reference"):
        render_prompt(assembly)


def test_join_and_render_context_sections():
    sections = [{"name": "fs", "text": "cwd: /x"}, {"name": "todo", "text": "a, b"}]
    joined = join_context_sections(sections)
    assert joined.startswith("Current runtime context.")
    assert "cwd: /x\n\na, b" in joined
    assert join_context_sections([]) == ""
    assembly = _assembly(contexts=[{"name": "fs", "text": "cwd: {{dir}}", "order": 0}], variables={"dir": "/x"})
    assert render_context_sections(assembly) == [{"name": "fs", "text": "cwd: /x"}]
    assert render_context_snapshot(assembly) == (
        "Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\ncwd: /x"
    )


# ---- 工具排序 ----


def test_order_tools_default_lexicographic():
    tools = [{"name": "zap"}, {"name": "apple"}]
    assert [t["name"] for t in order_tools(tools, None, set())] == ["apple", "zap"]


def test_order_tools_applies_tool_order():
    tools = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    ordered = order_tools(tools, ["c", TOOL_ORDER_REST, "a"], {"a", "b", "c"})
    assert [t["name"] for t in ordered] == ["c", "b", "a"]


def test_order_tools_unknown_name_throws():
    with pytest.raises(RuntimeError, match="unregistered tool"):
        order_tools([{"name": "a"}], [TOOL_ORDER_REST, "ghost"], {"a"})


def test_order_tools_reserved_provider_throws():
    with pytest.raises(RuntimeError, match="reserved tool name"):
        order_tools([{"name": TOOL_ORDER_REST}], None, set())


def test_validate_tool_order_duplicates_and_rest():
    with pytest.raises(RuntimeError, match="more than once"):
        validate_tool_order(["a", "a", TOOL_ORDER_REST])
    with pytest.raises(RuntimeError, match="must contain"):
        validate_tool_order(["a"])
    assert validate_tool_order(None) is None


# ---- 服务面 ----


@pytest.fixture
def ctx():
    return Context()


@pytest.fixture
def service(ctx):
    return SystemPrompt(ctx)


def test_default_sections(service):
    """harness 身份 + 人格槽默认在场。"""
    names = [s["name"] for s in service.layers.merge(None, lambda l: l.sections).values()]
    assert names == ["harness:identity", PERSONA_SECTION]


def test_duplicate_section_rejected(service):
    service.section({"name": "dup", "order": 1, "text": "x"})
    with pytest.raises(RuntimeError, match="already registered"):
        service.section({"name": "dup", "order": 2, "text": "y"})


def test_nonfinite_order_rejected(service):
    with pytest.raises(TypeError, match="finite"):
        service.section({"name": "bad", "order": float("nan"), "text": "x"})


def test_assemble_orders_and_shadows():
    ctx = Context()
    service = SystemPrompt(ctx)
    service.section({"name": "tools:guide", "order": 100, "text": "use tools"})
    service.context({"name": "cwd", "order": 0, "text": "cwd: {{dir}}", "text_fn": None})
    service.variable("dir", lambda _: "/workspace")
    service.tools(lambda _: {"schemas": [{"name": "bash", "description": "run", "parameters": {}}]})

    # 作用域注册影子全局同名分节(参考实现 的 ScopeKey 是活对象,须可弱引用)
    class Key:
        pass

    scoped = create_scope(ctx, Key())
    # 通过作用域 ctx 注册同分节名:merge 后最近作用域赢
    scope_layer = service.layers.effect(
        scoped.ctx,
        lambda layer: layer.sections.insert(PERSONA_SECTION, {"name": PERSONA_SECTION, "order": PERSONA_ORDER, "text": "scoped persona"}),
        "test",
    )

    assembly = asyncio.run(service.assemble({"scope": scope_of(scoped.ctx)}))
    by_name = {s["name"]: s for s in assembly["sections"]}
    assert by_name[PERSONA_SECTION]["text"] == "scoped persona"
    # 顺序:harness(-100) < persona(0) < tools(100)
    assert [s["name"] for s in assembly["sections"]] == ["harness:identity", PERSONA_SECTION, "tools:guide"]
    # 装配只解析文本,插值发生在渲染阶段(render_context_sections)
    assert assembly["contexts"] == [{"name": "cwd", "text": "cwd: {{dir}}"}]
    assert [t["name"] for t in assembly["tools"]] == ["bash"]
    scope_layer()


def test_assemble_waterfall_authoritative():
    ctx = Context()
    service = SystemPrompt(ctx)
    seen = []

    async def rewrite(assembly, context, next_):
        seen.append(assembly)
        assembly["sections"] = [{"name": "rewritten", "text": "by expert"}]
        return assembly

    ctx.on("system-prompt/assemble", rewrite)
    assembly = asyncio.run(service.assemble())
    assert seen and assembly["sections"] == [{"name": "rewritten", "text": "by expert"}]
    # 装配结果不变式:重复名被拒
    bad = dict(assembly)
    bad["sections"] = [{"name": "a", "text": "1"}, {"name": "a", "text": "2"}]
    with pytest.raises(RuntimeError, match="duplicated"):
        from system_prompt.system_prompt.src.index import validate_assembly

        validate_assembly(bad)


def test_assemble_complete_section_restored():
    ctx = Context()
    service = SystemPrompt(ctx)
    service.section({"name": "complete", "order": 50, "text": "whole prompt", "complete": True})
    assembly = asyncio.run(service.assemble())
    assert [s["name"] for s in assembly["sections"]] == ["complete"]


def test_assemble_multiple_complete_rejected(service):
    service.section({"name": "c1", "order": 1, "text": "a", "complete": True})
    service.section({"name": "c2", "order": 2, "text": "b", "complete": True})
    with pytest.raises(RuntimeError, match="multiple complete"):
        asyncio.run(service.assemble())


def test_suppress_runtime_context(service):
    service.context({"name": "c", "order": 0, "text": "ctx"})
    service.suppress_runtime_context()
    assembly = asyncio.run(service.assemble())
    assert assembly["contexts"] == []


def test_variable_invalid_name(service):
    with pytest.raises(RuntimeError, match="invalid prompt variable name"):
        service.variable("1bad", lambda _: "x")
