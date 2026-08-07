"""if 条件求值测试(§9.1 test_if_rules.py):语法解析 / Bash / 文件工具 / 恒 false / 事件剔除。

执行器集成(if 不匹配 → 不 spawn)属 S5 test_manager.py 范围,此处只测求值本身。
"""

from codesage.hooks._common import if_rule_matches, parse_hook_config
from codesage.tools import ToolRegistry, get_builtin_tools


def make_registry() -> ToolRegistry:
    return ToolRegistry(get_builtin_tools())


# ---------------------------------------------------------------------------
# 语法解析(§2.4:parse_rule 复用,括号不闭合恒 false)


def test_parse_syntax_shapes():
    r = make_registry()
    assert if_rule_matches("Bash(git *)", "Bash", {"command": "git status"}, r)
    assert if_rule_matches("Read(/src/**)", "Read", {"file_path": "/src/a/b.txt"}, r)
    assert if_rule_matches("Bash", "Bash", {"command": "anything"}, r)  # 裸工具名
    assert not if_rule_matches("Bash(git *", "Bash", {"command": "git status"}, r)  # 括号不闭合
    assert not if_rule_matches("(x)", "Bash", {"command": "x"}, r)  # 无工具名


# ---------------------------------------------------------------------------
# Bash 内容规则(§2.4 差异如实标注:字符串前缀,非 CC tree-sitter 语句级)


def test_bash_exact_match():
    r = make_registry()
    assert if_rule_matches("Bash(git status)", "Bash", {"command": "git status"}, r)
    assert not if_rule_matches("Bash(git status)", "Bash", {"command": "git log"}, r)


def test_bash_prefix_wildcard():
    r = make_registry()
    assert if_rule_matches("Bash(git *)", "Bash", {"command": "git push"}, r)
    assert not if_rule_matches("Bash(git *)", "Bash", {"command": "rm -rf /"}, r)


def test_bash_whitespace_normalization():
    """空格归一化(bash_rule_matches 内建):规则与命令的多余空白等价。"""
    r = make_registry()
    assert if_rule_matches("Bash(git   status)", "Bash", {"command": "git status"}, r)
    assert if_rule_matches("Bash(git *)", "Bash", {"command": "git  log  --oneline"}, r)


def test_bash_rule_on_non_bash_tool_false():
    r = make_registry()
    assert not if_rule_matches("Bash(git *)", "Read", {"file_path": "/x"}, r)


# ---------------------------------------------------------------------------
# 文件工具(§2.4:file_path 精确 / /** / /*;LS/Glob/Grep 用 path)


def test_file_tool_exact_and_recursive():
    r = make_registry()
    assert if_rule_matches("Read(/src/a.py)", "Read", {"file_path": "/src/a.py"}, r)
    assert if_rule_matches("Read(/src/**)", "Read", {"file_path": "/src/a/b/c.py"}, r)
    assert if_rule_matches("Read(/src/**)", "Read", {"file_path": "/src/a.py"}, r)
    # 单层 /* 不递归
    assert if_rule_matches("Read(/src/*)", "Read", {"file_path": "/src/a.py"}, r)
    assert not if_rule_matches("Read(/src/*)", "Read", {"file_path": "/src/a/b.py"}, r)
    # 目录前缀(rule 非通配时前缀匹配)
    assert if_rule_matches("Read(/src)", "Read", {"file_path": "/src/a.py"}, r)
    assert not if_rule_matches("Read(/src)", "Read", {"file_path": "/other/a.py"}, r)


def test_file_tool_read_write_set_separation():
    """读/写集分组(rules.py 同语义):Read 规则命中读集,Write 规则命中写集。"""
    r = make_registry()
    assert if_rule_matches("Read(/src/**)", "Grep", {"path": "/src/a/b.py"}, r)
    assert if_rule_matches("Write(/src/**)", "Edit", {"file_path": "/src/x.py"}, r)
    assert not if_rule_matches("Read(/src/**)", "Write", {"file_path": "/src/x.py"}, r)
    assert not if_rule_matches("Write(/src/**)", "Read", {"file_path": "/src/x.py"}, r)


def test_search_tools_use_path_field():
    """LS/Glob/Grep 的匹配字段是 path(可选,缺省 cwd);缺失 → 恒 false,不猜测 cwd(§2.4)。"""
    r = make_registry()
    assert if_rule_matches("Read(/src/**)", "Glob", {"path": "/src/a/b.py"}, r)
    assert if_rule_matches("Read(/src/**)", "LS", {"path": "/src/a.py"}, r)
    assert not if_rule_matches("Read(/src/**)", "Glob", {"pattern": "**/*.py"}, r)  # 无 path
    assert not if_rule_matches("Read(/src/**)", "Grep", {}, r)


def test_file_path_missing_false():
    """文件工具路径字段缺失 → 恒 false:经 _path_field 取不到非空 str 即 false(§2.4,不猜测 cwd)。"""
    r = make_registry()
    assert not if_rule_matches("Read(/src/**)", "Read", {}, r)
    assert not if_rule_matches("Write(/src/**)", "Write", {"content": "x"}, r)
    assert not if_rule_matches("Edit(/src/**)", "Edit", {"old_string": "a"}, r)
    assert not if_rule_matches("Read(/src/**)", "Read", {"file_path": ""}, r)


# ---------------------------------------------------------------------------
# 恒 false 与工具级匹配


def test_tool_not_exists_false():
    """工具不存在(registry.get 为 None)→ 恒 false(§2.4 第 3 条)。"""
    r = make_registry()
    assert not if_rule_matches("Bash(git *)", "Nope", {"command": "git status"}, r)
    assert not if_rule_matches("Bash", "Nope", {}, r)


def test_tool_name_mismatch_false():
    """规则工具名与实际工具不匹配 → false。"""
    r = make_registry()
    assert not if_rule_matches("Read(/src/**)", "Bash", {"command": "git"}, r)
    assert not if_rule_matches("WebFetch(foo)", "Bash", {"command": "git"}, r)


def test_validate_input_error_false():
    """validate_input 抛错 → 恒 false(§2.4 第 3 条;base 空实现,此处 Bash 覆写真实抛错)。"""
    r = make_registry()
    assert not if_rule_matches("Bash(git *)", "Bash", {"command": ""}, r)
    assert not if_rule_matches("Bash", "Bash", {"command": "   "}, r)


def test_other_tool_content_rule_tool_level():
    """其他工具(WebFetch…)无内容字段,内容规则退化为工具级匹配(§2.4 与 rules.py 同语义)。"""
    r = make_registry()
    assert if_rule_matches("WebFetch(anything)", "WebFetch", {"url": "https://x"}, r)
    assert not if_rule_matches("WebFetch(anything)", "TodoWrite", {"todos": []}, r)
    # 裸工具名精确匹配
    assert if_rule_matches("TodoWrite", "TodoWrite", {"todos": []}, r)
    assert not if_rule_matches("TodoWrite", "WebFetch", {"url": "https://x"}, r)


# ---------------------------------------------------------------------------
# 事件剔除(§2.4:非 PreToolUse/PostToolUse 带 if → warning + 永不执行)


def test_non_evaluable_event_if_warns_never_runs():
    """配置解析:Stop 事件带 if → warning,钩子保留但 if_evaluable=False(永不执行)。"""
    warnings: list[str] = []
    parsed = parse_hook_config(
        {"Stop": [{"hooks": [{"type": "command", "command": "c", "if": "Bash(git *)"}]}]},
        warn=warnings.append,
    )
    assert any("cannot evaluate 'if'" in w for w in warnings)
    spec = parsed["Stop"][0].hooks[0]
    assert spec.if_evaluable is False


def test_evaluable_events_keep_if():
    """PreToolUse/PostToolUse 的 if 可求值(if_evaluable=True)。"""
    for event in ("PreToolUse", "PostToolUse"):
        parsed = parse_hook_config(
            {event: [{"hooks": [{"type": "command", "command": "c", "if": "Bash(git *)"}]}]}
        )
        assert parsed[event][0].hooks[0].if_evaluable is True
