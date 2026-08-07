"""命令执行体(阶段 09,S3):shell 子进程执行,§4.1-§4.6。

复用 tools/builtin/shell/bash.py 的 `_shell_argv`(Windows 优先 Git Bash,§4.1)
与 `kill_tree`(Windows taskkill /T 树杀,§11)——只读复用,bash.py 本身不改动。
stdout/stderr 捕获限额与 `{` 前缀 JSON 解析分支(§4.3/§4.10.5)在本模块;§4.6
的 fail-closed 语义(PreToolUse → deny)由 S5 HookManager 在结果之上执行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

from ..tools.builtin.shell import bash
from .base import HookResult
from .types import HookJSONOutput

logger = logging.getLogger("codesage.hooks")

#: stdout 捕获限额(§4.10.5):超限截断(保留前 256KB + 截断标记入日志)。
#: 截断的 JSON 自然解析失败 → fail-closed;plainText 截断仅日志。
MAX_STDOUT_BYTES = 256 * 1024
#: stderr 截断(§4.5):>2000 字符裁剪,防超大输出(stderr 是给人看的摘要)。
MAX_STDERR_CHARS = 2000


class HookExecutionError(RuntimeError):
    """执行体级失败(§4.1/§4.6):spawn 失败 / stdin 提前关闭 / shell 中介下 exit 127。

    HookManager 捕获后按 §4.6 表处理:PreToolUse → deny,其他事件 → 非阻塞错误。
    127(命令不存在)按 spawn 失败同判(fail-closed,§4.6 注,与 CC 非阻塞取向刻意分歧)。
    """


def classify_exit_code(code: int) -> str:
    """退出码分类(§4.10.5):0 = success / 2 = blocked / 1 及其他 = non_blocking_error。

    返回值即 HOOK_OUTCOMES 取值,直接进 HookAuditEvent.outcome(§8.1)。
    run() 已在分类前拦截 127(spawn-failure 语义 → HookExecutionError,§4.6 注),
    此处仅分类正常完成执行的退出码。
    """
    if code == 0:
        return "success"
    if code == 2:
        return "blocked"
    return "non_blocking_error"


def parse_hook_stdout(stdout: str, event: str) -> tuple[HookJSONOutput | None, list[str]]:
    """stdout 解析分支(§4.3/§4.10.5):不以 `{` 开头 → plainText(返回 None,仅日志);
    以 `{` 开头 → JSON 解析 + schema 校验(§4.4),失败抛 HookValidationError
    (调用方按 §4.6 fail-closed:PreToolUse → deny)。

    前缀判定对前导空白宽容(lstrip):宁可进解析分支失败关门,不可把畸形 JSON 当
    plainText 静默放过(§11「钩子输出不被信任」)。
    """
    if not stdout.lstrip().startswith("{"):
        return None, []
    return HookJSONOutput.parse(stdout, event)


def _input_cwd(input_json: str) -> str | None:
    """从 HookInput JSON 取 cwd(§4.1:子进程 cwd 与 CODESAGE_PROJECT_DIR 的来源)。

    输入是 HookManager 序列化的内部 JSON(§4.10.4),解析失败属防御性兜底,不抛错。
    """
    try:
        cwd = json.loads(input_json).get("cwd")
    except (json.JSONDecodeError, AttributeError):
        return None
    return cwd if isinstance(cwd, str) and cwd else None


def _cap_stdout(stdout_b: bytes) -> str:
    """stdout 捕获限额(§4.10.5):>256KB 截断 + UTF-8 errors=replace 解码。

    字节截断后按 UTF-8 errors=replace 解码(§4.10.5)——GBK 等非 UTF-8 输出 → 替换符,
    不抛错不中断;乱码原文进 JSON 解析必然校验失败,不存在「乱码被当合法 JSON」路径。
    """
    if len(stdout_b) > MAX_STDOUT_BYTES:
        logger.warning("hook stdout exceeded %d bytes: truncated (§4.10.5)", MAX_STDOUT_BYTES)
        stdout_b = stdout_b[:MAX_STDOUT_BYTES]
    return stdout_b.decode("utf-8", errors="replace")


async def _run_and_collect(
    proc: asyncio.subprocess.Process, stdin_data: bytes
) -> tuple[bytes, bytes]:
    """喂入 stdin(§4.1:HookInput JSON + 换行)并收齐 stdout/stderr。

    手动喂入而非 communicate(input=...):communicate 会静默吞掉 BrokenPipeError,
    而 §4.1 要求「stdin 提前关闭视为错误」。
    """
    proc.stdin.write(stdin_data)
    await proc.stdin.drain()
    proc.stdin.close()
    return await proc.communicate()


class CommandHookExecutor:
    """命令执行体(§4.1-§4.6):shell 子进程 + stdin JSON + 超时杀树 + 输出限额。

    与 bash 工具同构:Windows 优先 Git Bash(bash._shell_argv)、超时树杀
    (bash.kill_tree)。超时 / spawn 失败 / stdin 提前关闭抛异常、不构造 HookResult
    (base.py 契约),HookManager 按 §4.6 fail-closed。退出码经 HookResult 原样返回,
    由调用方 classify_exit_code + parse_hook_stdout 消费(§4.10.5)。
    """

    def __init__(self, command: str):
        self.command = command

    async def run(self, input_json: str, *, timeout: float) -> HookResult:
        cwd = _input_cwd(input_json)
        env = os.environ.copy()
        if cwd is not None:
            env["CODESAGE_PROJECT_DIR"] = cwd
        argv = bash._shell_argv(self.command)
        try:
            if argv is not None:
                proc = await asyncio.create_subprocess_exec(
                    *argv[0],
                    cwd=cwd,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=sys.platform != "win32",
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    self.command,
                    cwd=cwd,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=sys.platform != "win32",
                )
        except OSError as exc:
            # 子进程启动失败(命令不存在等,§4.6 表第三行)
            raise HookExecutionError(
                f"failed to spawn hook process: {exc}"
            ) from exc

        started = time.monotonic()
        try:
            # 单个 wait_for 同时覆盖 stdin 喂入与 communicate:子进程不读 stdin 时
            # drain 也会阻塞,同样受超时约束(§4.2)
            stdout_b, stderr_b = await asyncio.wait_for(
                _run_and_collect(proc, input_json.encode("utf-8") + b"\n"), timeout
            )
        except asyncio.TimeoutError:
            # 超时 → 杀进程树,按 §4.6 fail-closed(§4.2;Windows 用 taskkill 树杀)
            bash.kill_tree(proc)
            await proc.wait()
            raise TimeoutError(
                f"hook timed out after {timeout:.1f}s: {self.command!r}"
            ) from None
        except (BrokenPipeError, ConnectionResetError) as exc:
            # §4.1:stdin 提前关闭视为错误
            bash.kill_tree(proc)
            await proc.wait()
            raise HookExecutionError(
                f"hook closed stdin early (§4.1): {self.command!r}"
            ) from exc
        except asyncio.CancelledError:
            bash.kill_tree(proc)
            await proc.wait()
            raise

        if proc.returncode == 127:
            # §4.6 注(lead 定案):shell 中介下 127 = 命令不存在,按 spawn 失败处理
            # → fail-closed(PreToolUse → deny 由 S5 消费)。代价:钩子脚本显式
            # exit 127 同判 —— 安全取向优先于与 CC 一致的非阻塞语义。
            raise HookExecutionError(
                f"hook command not found (exit 127): {self.command!r}"
            )

        return HookResult(
            exit_code=proc.returncode,
            stdout=_cap_stdout(stdout_b),
            stderr=stderr_b.decode("utf-8", errors="replace")[:MAX_STDERR_CHARS],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
