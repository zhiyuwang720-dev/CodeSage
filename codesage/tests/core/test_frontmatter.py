"""core/frontmatter 共享解析器测试(阶段 14 S1)。

覆盖从 agents/loader.py 提取前的解析行为(标量/列表/map/坏文件/BOM)与
list_fields / map_fields 参数化扩展 —— 提取是行为保持重构,13 的
agents loader 测试(经 load_dir)同步兜底逐位回归。
"""

from codesage.core.frontmatter import (
    parse_flow_list,
    parse_flow_map,
    parse_frontmatter,
    parse_scalar,
)


def test_parse_scalar_forms():
    """标量解析:引号去除 + YAML 1.1 布尔 + 整数,非整数保持字符串。"""
    assert parse_scalar("hello") == "hello"
    assert parse_scalar('"quoted"') == "quoted"
    assert parse_scalar("'single'") == "single"
    assert parse_scalar("true") is True
    assert parse_scalar("yes") is True
    assert parse_scalar("on") is True
    assert parse_scalar("no") is False
    assert parse_scalar("off") is False
    assert parse_scalar("null") is None
    assert parse_scalar("~") is None
    assert parse_scalar("42") == 42
    assert parse_scalar("4.2") == "4.2"  # 非整数标量保持字符串


def test_parse_flow_list_forms():
    """flow 列表三种形态:[a, b] / 逗号 / 空格,引号可剥。"""
    assert parse_flow_list("[a, b, c]") == ["a", "b", "c"]
    assert parse_flow_list("a, b, c") == ["a", "b", "c"]
    assert parse_flow_list("a b c") == ["a", "b", "c"]
    assert parse_flow_list("'a' \"b\"") == ["a", "b"]
    assert parse_flow_list("") == []


def test_parse_flow_map():
    """单行 flow map:值仅标量。"""
    assert parse_flow_map("{PreToolUse: echo a}") == {"PreToolUse": "echo a"}
    assert parse_flow_map("a: 1, b: x") == {"a": 1, "b": "x"}


def test_parse_frontmatter_basic():
    """基本解析:返回 (dict, 结束围栏之后行号),正文行号正确。"""
    text = "---\nname: a\ndescription: d\ntools: [Read, Bash]\n---\nbody text\n"
    fm, start = parse_frontmatter(text, list_fields=frozenset({"tools"}))
    assert fm == {"name": "a", "description": "d", "tools": ["Read", "Bash"]}
    assert start == 5
    assert text.splitlines()[start] == "body text"


def test_parse_frontmatter_no_fence():
    """无围栏 / 未闭合围栏 → None(调用方跳过)。"""
    assert parse_frontmatter("name: a\n") is None
    assert parse_frontmatter("---\nname: a\n") is None


def test_parse_frontmatter_bom():
    """BOM 容忍(Windows 编辑器常见)。"""
    fm, _ = parse_frontmatter("\ufeff---\nname: a\n---\n")
    assert fm["name"] == "a"


def test_parse_frontmatter_fence_inside_body_kept():
    """正文里的 --- 保持正文文本(单趟扫描)。"""
    text = "---\nname: a\n---\nbody one\n---\nbody two\n"
    fm, start = parse_frontmatter(text)
    assert fm["name"] == "a"
    assert text.splitlines()[start] == "body one"


def test_parse_frontmatter_unknown_key_stays_scalar():
    """非 list/map 字段不按结构化解析(白名单过滤在调用方)。"""
    text = "---\nname: a\nmcpServers: {x: 1}\n---\n"
    fm, _ = parse_frontmatter(text)
    assert fm["name"] == "a"
    assert fm["mcpServers"] == "{x: 1}"  # 标量原样


def test_parse_frontmatter_one_level_map():
    """缩进单层 map(如 hooks):值仅标量,空行/注释行跳过。"""
    text = "---\nname: a\nhooks:\n  PreToolUse: echo a\n\n  PostToolUse: echo b\n---\n"
    fm, _ = parse_frontmatter(text, map_fields=frozenset({"hooks"}))
    assert fm["hooks"] == {"PreToolUse": "echo a", "PostToolUse": "echo b"}


def test_parse_frontmatter_map_flow_form():
    """map 字段的 flow 形态:{k: v}。"""
    text = "---\nname: a\nhooks: {PreToolUse: echo a}\n---\n"
    fm, _ = parse_frontmatter(text, map_fields=frozenset({"hooks"}))
    assert fm["hooks"] == {"PreToolUse": "echo a"}


def test_list_fields_parameterized():
    """同一键在不同调用方可声明为 list 或标量(参数化扩展)。"""
    text = "---\nallowed-tools: [Read, Grep]\n---\n"
    fm, _ = parse_frontmatter(text, list_fields=frozenset({"allowed-tools"}))
    assert fm["allowed-tools"] == ["Read", "Grep"]
    fm2, _ = parse_frontmatter(text)
    assert fm2["allowed-tools"] == "[Read, Grep]"  # 默认标量


def test_map_fields_parameterized():
    """map 字段集也可按调用方声明(默认含 hooks,但可扩展)。"""
    text = "---\nskills:\n  a: 1\n  b: 2\n---\n"
    fm, _ = parse_frontmatter(text, map_fields=frozenset({"hooks", "skills"}))
    assert fm["skills"] == {"a": 1, "b": 2}


def test_comment_and_blank_lines_skipped():
    """frontmatter 内注释行与空行跳过。"""
    text = "---\n# 注释\nname: a\n\ndescription: d\n---\n"
    fm, _ = parse_frontmatter(text)
    assert fm == {"name": "a", "description": "d"}
