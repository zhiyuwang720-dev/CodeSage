"""技能提示词管道(阶段 14 §5.1):四阶段流水线。

```mermaid
Raw[body 正文] --> Base["1. 基础目录前缀"]
Base --> Args["2. 参数替换 substitute_arguments"]
Args --> Env["3. 环境变量替换"]
Env --> Shell["4. 内联 Shell 执行(仅本地技能)"]
Shell --> Final[最终提示词]
```

原始 Markdown 不直接送模型:先插基础目录前缀,再做参数/环境变量替换,
最后对本地技能执行内联 shell 块(spec 14 §5.1)。``loop`` 是 AgentLoop 兼容
对象 —— 阶段 14 S4 为引擎补 ``skill_allowed_tools`` 后即接真实引擎,测试
用假 loop 承载。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .shell import SkillPromptError, execute_shell_blocks
from .types import SkillDefinition

if TYPE_CHECKING:
    from ..engine.loop import AgentLoop

#: 参数占位符:``$ARGUMENTS`` / ``$file`` / ``$0..$n`` / ``$ARGUMENTS[n]``
_ARGUMENTS_INDEX_RE = re.compile(r"\$ARGUMENTS\[(\d+)\]")
_ARGUMENTS_ALL_RE = re.compile(r"\$ARGUMENTS\b")
_FILE_RE = re.compile(r"\$file\b")
_POSITION_RE = re.compile(r"\$([1-9]\d*|0)\b")


def substitute_arguments(text: str, args: str, *, arguments: tuple[str, ...] = ()) -> str:
    """参数替换(CC argumentSubstitution.ts 对齐,spec §5.1 Step 2)。

    - ``$ARGUMENTS`` → 全部参数字符串(空白切分后重连);
    - ``$file`` → 命名参数按位映射 = 首个参数(args[0]);仅支持无花括号形,
      ``${file}`` 不识别(花括号形仅内置环境变量);
    - ``$0``/``$1`` → 按位置索引替换(0 基);
    - ``$ARGUMENTS[n]`` → 按索引访问;
    - 提示词中无任何占位符 → 参数自动追加到末尾(``ARGUMENTS: {args}``)。

    ``arguments`` 仅为命名参数语义声明(frontmatter arguments 列表),替换
    本身按位置求值;越界索引 → 空串(CC 同款)。
    """
    arg_list = args.split() if args else []
    has_placeholder = {"found": False}

    def _at(index: int) -> str:
        return arg_list[index] if 0 <= index < len(arg_list) else ""

    def _replacer(fn: Any):
        def repl(match: re.Match) -> str:
            has_placeholder["found"] = True
            return fn(match)

        return repl

    out = _ARGUMENTS_INDEX_RE.sub(_replacer(lambda m: _at(int(m.group(1)))), text)
    out = _ARGUMENTS_ALL_RE.sub(_replacer(lambda _m: " ".join(arg_list)), out)
    out = _FILE_RE.sub(_replacer(lambda _m: _at(0)), out)
    out = _POSITION_RE.sub(_replacer(lambda m: _at(int(m.group(1)))), out)
    if not has_placeholder["found"] and args:
        out = out.rstrip() + f"\n\nARGUMENTS: {args}"
    return out


def substitute_env_vars(text: str, *, skill_dir: Path | None, session_id: str) -> str:
    """环境变量替换(§5.1 Step 3):``${CODESAGE_SKILL_DIR}`` /
    ``${CODESAGE_SESSION_ID}``。

    前缀沿用 CodeSage 既有 ``CODESAGE_`` 约定(config/paths.py 同款);
    skill_dir 的 Windows 反斜杠转正斜杠(模型上下文友好)。
    """
    out = text.replace(
        "${CODESAGE_SKILL_DIR}",
        str(skill_dir).replace("\\", "/") if skill_dir is not None else "",
    )
    return out.replace("${CODESAGE_SESSION_ID}", session_id)


def base_dir_prefix(skill: SkillDefinition) -> str:
    """Step 1 基础目录前缀:``skill_dir`` 存在时前置,否则空串。"""
    if skill.skill_dir is None:
        return ""
    return f"Base directory for this skill: {skill.skill_dir}"


async def get_prompt_for_command(
    skill: SkillDefinition,
    args: str,
    *,
    session_id: str,
    cwd: Path,
    loop: "AgentLoop",
) -> str:
    """按技能定义解析出最终提示词(§5.1 四阶段流水线)。

    内联 shell 块只对**本地技能**执行(builtin 内容是受信 Python 字符串,
    无注入面;spec §1.2/§5.2 成文)。解析失败(含 shell 权限拒绝)抛
    :class:`SkillPromptError` —— 双路径调用方各自转为对应错误形态。
    """
    prompt = base_dir_prefix(skill)
    body = substitute_arguments(skill.body, args, arguments=skill.arguments)
    body = substitute_env_vars(body, skill_dir=skill.skill_dir, session_id=session_id)
    if skill.source != "builtin":
        body = await execute_shell_blocks(
            body, loop=loop, skill_allowed_tools=skill.allowed_tools
        )
    return f"{prompt}\n\n{body}".strip() if prompt else body.strip()
