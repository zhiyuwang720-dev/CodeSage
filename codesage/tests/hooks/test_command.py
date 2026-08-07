"""命令执行体测试(§9.1 test_command.py):子进程执行(stdin JSON / env / cwd)、stdout JSON
解析与 plainText 分支、退出码 0/1/2/其他 全表、超时 fail-closed、spawn 失败、
stderr 捕获与截断、Windows Git Bash 选择(_shell_argv 复用)。"""

import json
import shutil
import sys
import time

import pytest

from codesage.hooks import HookValidationError
from codesage.hooks.command import (
    MAX_STDERR_CHARS,
    MAX_STDOUT_BYTES,
    CommandHookExecutor,
    HookExecutionError,
    _input_cwd,
    classify_exit_code,
    parse_hook_stdout,
)
from codesage.tools.builtin.shell import bash as bash_mod


def _hook_py(tmp_path, code: str) -> str:
    """把 Python 脚本写入 tmp_path,返回 hook command(正斜杠路径,sh/cmd 均可解释)。

    脚本内容经文件传递、不经 shell 引号,命令串只需引住两个路径。
    """
    script = tmp_path / "hook_script.py"
    script.write_text(code, encoding="utf-8")
    py = sys.executable.replace("\\", "/")
    return f'"{py}" "{script}"'


def _input_json(tmp_path) -> str:
    """HookInput 序列化(cwd 指向真实存在的 tmp_path,子进程以它为工作目录)。"""
    return json.dumps(
        {"session_id": "s1", "cwd": str(tmp_path), "session_path": str(tmp_path / "s.jsonl")}
    )


# ---------------------------------------------------------------------------
# 子进程执行(§4.1):stdin JSON / env / cwd


async def test_stdin_json_passed_to_hook(tmp_path):
    """§4.1:stdin = HookInput JSON + 换行;cat 原样回读验证。"""
    input_json = _input_json(tmp_path)
    r = await CommandHookExecutor("cat").run(input_json, timeout=10)
    assert r.exit_code == 0
    assert r.stdout == input_json + "\n"


async def test_env_and_cwd(tmp_path):
    """§4.1:环境继承 + CODESAGE_PROJECT_DIR(= 输入 cwd);子进程 cwd = 输入 cwd。"""
    script = (
        "import os\n"
        "print(os.environ.get('CODESAGE_PROJECT_DIR', '<unset>'))\n"
        "print(os.getcwd())\n"
    )
    r = await CommandHookExecutor(_hook_py(tmp_path, script)).run(_input_json(tmp_path), timeout=10)
    assert r.exit_code == 0
    lines = r.stdout.strip().splitlines()
    assert lines[0] == str(tmp_path)  # CODESAGE_PROJECT_DIR
    assert lines[1] == str(tmp_path)  # os.getcwd()


# ---------------------------------------------------------------------------
# stdout 解析与 plainText 分支(§4.3/§4.10.5)


async def test_stdout_json_parsed(tmp_path):
    """§4.3:stdout 以 `{` 开头 → JSON 解析 + schema 校验。"""
    script = (
        "import json, sys; "
        "print(json.dumps({'permissionDecision': 'deny', 'permissionDecisionReason': 'r'}))"
    )
    r = await CommandHookExecutor(_hook_py(tmp_path, script)).run(_input_json(tmp_path), timeout=10)
    out, warnings = parse_hook_stdout(r.stdout, "PreToolUse")
    assert warnings == []
    assert out.permissionDecision == "deny"
    assert out.permissionDecisionReason == "r"


async def test_stdout_plain_text_not_parsed(tmp_path):
    """§4.3:不以 `{` 开头 → plainText(返回 None,仅日志);空 stdout 同。"""
    r = await CommandHookExecutor(_hook_py(tmp_path, "print('hello plain text')")).run(
        _input_json(tmp_path), timeout=10
    )
    assert parse_hook_stdout(r.stdout, "PreToolUse") == (None, [])
    r2 = await CommandHookExecutor(_hook_py(tmp_path, "pass")).run(_input_json(tmp_path), timeout=10)
    assert parse_hook_stdout(r2.stdout, "PreToolUse") == (None, [])


def test_parse_hook_stdout_leading_whitespace_parses():
    """§4.3/§4.10.5:前导空白不逃逸解析分支 —— `\\n{...}` 仍按 JSON 解析,不当 plainText。"""
    out, _ = parse_hook_stdout('\n{"permissionDecision": "allow"}', "PreToolUse")
    assert out is not None
    assert out.permissionDecision == "allow"


def test_input_cwd_malformed_json_defensive():
    """防御路径:畸形 input_json → cwd=None(不抛错,子进程继承调用方 cwd)。"""
    assert _input_cwd("not json at all") is None
    assert _input_cwd('["a", "b"]') is None
    assert _input_cwd('{"cwd": 123}') is None
    assert _input_cwd('{"cwd": "/real"}') == "/real"


async def test_stdout_invalid_json_fail_closed(tmp_path):
    """§4.6:以 `{` 开头但 JSON 非法 → HookValidationError(fail-closed 依据)。"""
    r = await CommandHookExecutor(_hook_py(tmp_path, "print('{not valid json')")).run(
        _input_json(tmp_path), timeout=10
    )
    with pytest.raises(HookValidationError):
        parse_hook_stdout(r.stdout, "PreToolUse")


async def test_stdout_event_mismatched_field_rejected(tmp_path):
    """§4.4:事件不匹配字段(Stop 事件带 permissionDecision)→ 校验失败。"""
    script = "import json, sys; print(json.dumps({'permissionDecision': 'deny'}))"
    r = await CommandHookExecutor(_hook_py(tmp_path, script)).run(_input_json(tmp_path), timeout=10)
    with pytest.raises(HookValidationError):
        parse_hook_stdout(r.stdout, "Stop")


async def test_stdout_truncated_at_256kb(tmp_path):
    """§4.10.5:stdout 超 256KB 截断;截断的 JSON → 解析失败(fail-closed 依据)。"""
    r = await CommandHookExecutor(_hook_py(tmp_path, "import sys; sys.stdout.write('{' + 'x' * 300_000)")).run(
        _input_json(tmp_path), timeout=10
    )
    assert len(r.stdout) == MAX_STDOUT_BYTES
    assert r.stdout.startswith("{")
    with pytest.raises(HookValidationError):
        parse_hook_stdout(r.stdout, "PreToolUse")


async def test_utf8_invalid_bytes_replaced(tmp_path):
    """§4.10.5:非 UTF-8 字节 errors=replace 解码不抛错;乱码不被当合法 JSON。"""
    script = "import sys; sys.stdout.buffer.write('你好'.encode('gbk') + b'{\"a\": 1}')"
    r = await CommandHookExecutor(_hook_py(tmp_path, script)).run(_input_json(tmp_path), timeout=10)
    assert r.stdout.startswith("\ufffd")
    # 替换符在前 → 不以 `{` 开头 → plainText 分支,不存在「乱码被当合法 JSON」路径
    assert parse_hook_stdout(r.stdout, "PreToolUse") == (None, [])


# ---------------------------------------------------------------------------
# 退出码全表(§4.3/§4.10.5)


@pytest.mark.parametrize(
    "code, expected",
    [
        (0, "success"),
        (2, "blocked"),
        (1, "non_blocking_error"),
        (7, "non_blocking_error"),
    ],
)
async def test_exit_code_table(tmp_path, code, expected):
    """退出码 0/1/2/其他 全表分类(§4.3 表同构)。"""
    r = await CommandHookExecutor(_hook_py(tmp_path, f"import sys; sys.exit({code})")).run(
        _input_json(tmp_path), timeout=10
    )
    assert r.exit_code == code
    assert classify_exit_code(r.exit_code) == expected


async def test_exit_2_stderr_is_blocking_error_source(tmp_path):
    """§4.3:exit 2 的 stderr 是 blockingError 内容源。"""
    r = await CommandHookExecutor(
        _hook_py(tmp_path, "import sys; print('denied: rule X', file=sys.stderr); sys.exit(2)")
    ).run(_input_json(tmp_path), timeout=10)
    assert r.exit_code == 2
    assert classify_exit_code(r.exit_code) == "blocked"
    assert r.stderr.strip() == "denied: rule X"


async def test_exit_127_spawn_failure_semantics(tmp_path):
    """§4.6 注:shell 中介下 127 = 命令不存在 → HookExecutionError(fail-closed)。

    真实路径:Git Bash /bin/sh 对不存在命令报 127,而 create_subprocess 的 shell 本体
    必然存在(OSError 分支覆盖不到)。「钩子脚本显式 exit 127」同判 —— 规格安全取向,
    与 CC 非阻塞取向刻意分歧(docs/specs/09-hooks.md §4.6 注)。
    """
    with pytest.raises(HookExecutionError, match="127"):
        await CommandHookExecutor(_hook_py(tmp_path, "import sys; sys.exit(127)")).run(
            _input_json(tmp_path), timeout=10
        )
    # 真实「命令不存在」路径:shell 报 127,同判 spawn 失败
    with pytest.raises(HookExecutionError, match="127"):
        await CommandHookExecutor("no_such_command_xyz_123").run(_input_json(tmp_path), timeout=10)


# ---------------------------------------------------------------------------
# 超时 / spawn 失败(fail-closed 依据,§4.2/§4.6)


async def test_timeout_kills_hung_hook(tmp_path):
    """挂起钩子 → 超时杀进程 + TimeoutError(管理器按 §4.6 fail-closed 处理)。"""
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await CommandHookExecutor(_hook_py(tmp_path, "import time; time.sleep(60)")).run(
            _input_json(tmp_path), timeout=1.0
        )
    # 超时后进程被杀、wait 完成才抛错,不会拖到 60s
    assert time.monotonic() - started < 15


async def test_spawn_failure_raises(tmp_path, monkeypatch):
    """§4.6:create_subprocess_exec 的 OSError 分支(spawn 启动失败)→ HookExecutionError。

    注:真实路径下 shell 本体必然存在,OSError 只覆盖窄竞态(如 cwd 失效);真实路径
    的「命令不存在」以 shell 退出码 127 浮现,由 test_exit_127_spawn_failure_semantics
    覆盖(§4.6 注,同判 fail-closed)。此处 monkeypatch 强制 argv 指向不存在的可执行文件。
    """
    monkeypatch.setattr(
        bash_mod, "_shell_argv", lambda cmd: ["C:/no_such_dir/zzz.exe", "-c", cmd]
    )
    with pytest.raises(HookExecutionError):
        await CommandHookExecutor("echo hi").run(_input_json(tmp_path), timeout=5)


async def test_stdin_early_close_is_error(tmp_path):
    """§4.1:子进程提前关闭 stdin → HookExecutionError(1MB 输入超管道缓冲,确定性触发)。

    小 payload 会先落入管道缓冲(64KB)而竞态通过(钩子即时退出不触发 EPIPE);只有
    显式提前关闭 stdin 或超大输入让 drain 阻塞才会触发 BrokenPipeError —— 本测试
    两者兼用:子进程 os.close(0) + 1MB 输入,确定性命中错误分支。
    """
    script = "import os; os.close(0)"
    big_input = json.dumps(
        {
            "session_id": "s1",
            "cwd": str(tmp_path),
            "session_path": str(tmp_path / "s.jsonl"),
            "blob": "x" * (1024 * 1024),
        }
    )
    with pytest.raises(HookExecutionError, match="stdin"):
        await CommandHookExecutor(_hook_py(tmp_path, script)).run(big_input, timeout=10)


# ---------------------------------------------------------------------------
# stderr 捕获与截断(§4.5)


async def test_stderr_truncated(tmp_path):
    """§4.5:stderr 超 2000 字符截断。"""
    r = await CommandHookExecutor(_hook_py(tmp_path, "import sys; sys.stderr.write('e' * 5000)")).run(
        _input_json(tmp_path), timeout=10
    )
    assert len(r.stderr) == MAX_STDERR_CHARS


# ---------------------------------------------------------------------------
# Windows Git Bash 选择(§4.1,_shell_argv 模式复用)


async def test_delegates_to_bash_shell_argv(tmp_path, monkeypatch):
    """§4.1:命令选择复用 bash._shell_argv(Windows 优先 Git Bash 的既有逻辑)。"""
    seen: list[str] = []
    original = bash_mod._shell_argv

    def spy(cmd):
        seen.append(cmd)
        return original(cmd)

    monkeypatch.setattr(bash_mod, "_shell_argv", spy)
    r = await CommandHookExecutor("echo hi").run(_input_json(tmp_path), timeout=10)
    assert seen == ["echo hi"]
    assert r.stdout.strip() == "hi"


async def test_posix_shell_semantics_with_git_bash(tmp_path):
    """§4.1:Windows 上优先 Git Bash,POSIX 语法($VAR 展开)经 shell 正常执行。"""
    if sys.platform == "win32" and shutil.which("bash") is None:
        pytest.skip("no Git Bash on Windows")
    r = await CommandHookExecutor("echo v=$HOOK_UNSET_VAR_XYZ").run(_input_json(tmp_path), timeout=10)
    # cmd.exe 会原样输出 $HOOK_UNSET_VAR_XYZ;sh/bash 展开为空 → "v="
    assert r.stdout.strip() == "v="
