"""技能提示词管道测试(阶段 14 S3):参数替换矩阵 / 环境变量替换 / base 前缀 /
内联 shell 块(双模式 / 并行 / 权限拒绝失败关闭 / 授权放行 / yolo / 函数式替换)。"""

import asyncio
from pathlib import Path

import pytest

from codesage.permissions import PermissionDecision
from codesage.skills import SkillDefinition
from codesage.skills.prompt import (
    base_dir_prefix,
    get_prompt_for_command,
    substitute_arguments,
    substitute_env_vars,
)
from codesage.skills.shell import SkillPromptError, execute_shell_blocks
from codesage.tools import ToolResult


# ---- 参数替换矩阵 ----

def test_substitute_arguments_all_forms():
    body = "$ARGUMENTS | $file | $0 | $1 | $2 | $ARGUMENTS[0] | $ARGUMENTS[2]"
    out = substitute_arguments(body, "alpha beta gamma")
    assert out == "alpha beta gamma | alpha | alpha | beta | gamma | alpha | gamma"


def test_substitute_arguments_missing_index_is_empty():
    body = "[$3][$ARGUMENTS[9]]"
    assert substitute_arguments(body, "a b") == "[][]"  # 越界 → 空串


def test_substitute_arguments_missing_index_order():
    """越界 $ARGUMENTS[n] → 空串(全量替换不吞掉索引形)。"""
    body = "[$ARGUMENTS[9]]"
    assert substitute_arguments(body, "a b") == "[]"


def test_substitute_arguments_no_placeholder_appends():
    body = "do something"
    out = substitute_arguments(body, "hello world")
    assert out == "do something\n\nARGUMENTS: hello world"


def test_substitute_arguments_no_placeholder_no_args():
    body = "do something"
    assert substitute_arguments(body, "") == "do something"


def test_substitute_arguments_braced_file_not_recognized():
    """${file} 不识别(花括号形仅内置环境变量)—— 原样保留。"""
    body = "${file} and $file"
    out = substitute_arguments(body, "x")
    assert out == "${file} and x"


def test_substitute_arguments_named_argument_semantics():
    """$file = 命名参数按位映射 = 首个参数(arguments 仅语义声明)。"""
    body = "read $file then $1"
    out = substitute_arguments(body, "a.py b.py", arguments=("file", "other"))
    assert out == "read a.py then b.py"


# ---- 环境变量替换 ----

def test_substitute_env_vars_dir_slashes_and_session():
    text = "dir=${CODESAGE_SKILL_DIR} sid=${CODESAGE_SESSION_ID}"
    out = substitute_env_vars(text, skill_dir=Path(r"C:\skills\foo"), session_id="s-123")
    assert out == "dir=C:/skills/foo sid=s-123"


def test_substitute_env_vars_no_skill_dir():
    out = substitute_env_vars("d=${CODESAGE_SKILL_DIR}", skill_dir=None, session_id="s")
    assert out == "d="


# ---- base 前缀 ----

def test_base_dir_prefix_present_and_absent():
    s = SkillDefinition(name="x", description="d", body="b",
                        skill_dir=Path("/tmp/skills/x"))
    assert base_dir_prefix(s) == f"Base directory for this skill: {Path('/tmp/skills/x')}"
    s2 = SkillDefinition(name="x", description="d", body="b")
    assert base_dir_prefix(s2) == ""


# ---- 假 loop / 假 Bash(权限 + 执行)----

class FakePermissions:
    """决策由传入的可调用 decide(cmd, skill_allowed_tools) 决定。"""

    def __init__(self, decide):
        self._decide = decide

    def evaluate_tool_use(self, *, tool_name, tool_input, tool, mode, cwd,
                          permissions, session_permissions, skill_allowed_tools=None):
        allowed = self._decide(str(tool_input.get("command") or ""), frozenset(skill_allowed_tools or ()))
        return PermissionDecision(allowed=allowed, mode="allow" if allowed else "deny", reason="test")


class FakeBashTool:
    name = "Bash"

    def __init__(self, outputs=None, default=None):
        self._outputs = outputs or {}
        self._default = default or "ok"

    async def call(self, input, ctx):
        cmd = str(input.get("command") or "")
        yield ToolResult(self._outputs.get(cmd, self._default))


def _loop(decide, *, bash=None):
    bash = bash or FakeBashTool()

    class L:
        pass

    loop = L()
    loop.tools = {"Bash": bash}
    loop.permissions = FakePermissions(decide)
    loop.mode = "default"
    loop.cwd = Path.cwd()
    loop.settings = None
    loop.session_permissions = None
    loop.abort = None
    loop._tool_ctx = None
    return loop


# ---- get_prompt_for_command 流水线 ----

def test_get_prompt_full_pipeline(tmp_path):
    """base 前缀 + 参数替换 + 环境变量替换 + shell 块执行,一次到位。"""
    skill_dir = tmp_path / "skills" / "greet"
    skill = SkillDefinition(
        name="greet", description="d",
        body="Hello ${CODESAGE_SESSION_ID}, $ARGUMENTS! See !`echo hi`",
        skill_dir=skill_dir,
    )
    loop = _loop(lambda cmd, granted: True)  # 全放行
    out = asyncio.run(get_prompt_for_command(
        skill, "world", session_id="s9", cwd=tmp_path, loop=loop,
    ))
    assert out.startswith(f"Base directory for this skill: {skill_dir}")
    assert "Hello s9, world!" in out
    assert "hi" in out  # shell 块输出已替换


def test_get_prompt_shell_denied_raises(tmp_path):
    skill = SkillDefinition(
        name="s", description="d",
        body="run !`dangerous`",
    )
    loop = _loop(lambda cmd, granted: False)  # 一律拒绝
    with pytest.raises(SkillPromptError, match="shell block denied"):
        asyncio.run(get_prompt_for_command(skill, "", session_id="s", cwd=tmp_path, loop=loop))


def test_get_prompt_builtin_skips_shell(tmp_path):
    """builtin 技能不执行 shell 块(受信 Python 字符串,无注入面)。"""
    skill = SkillDefinition(name="s", description="d", body="keep !`cmd`", source="builtin")
    loop = _loop(lambda cmd, granted: False)  # 若执行必被拒
    out = asyncio.run(get_prompt_for_command(skill, "", session_id="s", cwd=tmp_path, loop=loop))
    assert out == "keep !`cmd`"  # 原样保留,未触发权限检查


# ---- execute_shell_blocks ----

def test_shell_fence_and_inline_dual_mode(tmp_path):
    text = "before ```!\necho A\n``` middle !`echo B` after"
    loop = _loop(lambda cmd, granted: True)
    out = asyncio.run(execute_shell_blocks(text, loop=loop, skill_allowed_tools=frozenset({"Bash"})))
    assert out == "before ok middle ok after"  # 两个块都被替换(FakeBash 输出 "ok")
    assert "echo A" not in out and "echo B" not in out


def test_shell_deny_fails_whole(tmp_path):
    """deny(非 allow)→ 整次技能调用失败。"""
    text = "a !`one` b !`two`"
    loop = _loop(lambda cmd, granted: cmd != "two")  # two 被拒
    with pytest.raises(SkillPromptError, match="shell block denied: two"):
        asyncio.run(execute_shell_blocks(text, loop=loop, skill_allowed_tools=frozenset()))


def test_shell_allowed_tools_grants(tmp_path):
    """allowed-tools: [Bash] 的技能其 shell 块经 §7.1 授权自动放行。"""
    text = "run !`cmd`"
    loop = _loop(lambda cmd, granted: "Bash" in granted)
    out = asyncio.run(execute_shell_blocks(text, loop=loop, skill_allowed_tools=frozenset({"Bash"})))
    assert out == "run ok"


def test_shell_parallel_execution(tmp_path):
    """并行执行:所有块都运行(完成顺序由 gather 保证)。"""
    seen = set()

    class Tracking(FakeBashTool):
        async def call(self, input, ctx):
            seen.add(str(input.get("command")))
            yield ToolResult("done")

    loop = _loop(lambda cmd, granted: True, bash=Tracking())
    text = "!`a` !`b` !`c`"
    out = asyncio.run(execute_shell_blocks(text, loop=loop))
    assert out == "done done done"
    assert seen == {"a", "b", "c"}


def test_shell_functional_replace_no_injection(tmp_path):
    """函数式替换:输出里的 $& $' 等特殊序列原样保留(防注入 R5)。"""
    bash = FakeBashTool(default="[$&][$'][$1]")
    loop = _loop(lambda cmd, granted: True, bash=bash)
    text = "x !`cmd` y"
    out = asyncio.run(execute_shell_blocks(text, loop=loop))
    assert out == "x [$&][$'][$1] y"


def test_shell_output_spilled_when_oversized(tmp_path):
    """输出超限 → 落盘 + 预览指针(复用 03 阈值,R6)。"""
    big = "X" * 100_001
    bash = FakeBashTool(default=big)
    loop = _loop(lambda cmd, granted: True, bash=bash)
    text = "!`cmd`"
    out = asyncio.run(execute_shell_blocks(text, loop=loop))
    assert "(result saved to " in out and "X" * 500 in out  # 预览
    assert len(out) < 1000  # 不再携带完整输出


def test_shell_no_marker_returns_unchanged(tmp_path):
    loop = _loop(lambda cmd, granted: True)
    text = "plain text without shell blocks"
    out = asyncio.run(execute_shell_blocks(text, loop=loop))
    assert out == text


def test_shell_unavailable_bash_tool_raises(tmp_path):
    loop = _loop(lambda cmd, granted: True, bash=None)
    loop.tools = {}
    with pytest.raises(SkillPromptError, match="Bash tool unavailable"):
        asyncio.run(execute_shell_blocks("!`cmd`", loop=loop))
