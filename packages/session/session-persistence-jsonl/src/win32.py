"""Windows 耐久命名空间辅助(JSONL 后端专用)。

POSIX 通过「创建目录项 + fsync 父目录」发布新日志;Windows 经 Node
暴露不了父目录 fsync 契约,因此走原生耐久命名空间原语:在目标目录
建临时对象,以 ``MoveFileExW(..., MOVEFILE_WRITE_THROUGH)`` 发布
(不带替换,也不带跨卷复制回退)。

DSH 用 koffi 做 FFI;Python 用标准库 ctypes(零新依赖)。惰性绑定
使非 Windows 进程永不加载 kernel32。
"""

from __future__ import annotations

import ctypes
import os
import stat as stat_module
import tempfile

MOVEFILE_WRITE_THROUGH = 0x00000008

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_NOT_SAME_DEVICE = 17
ERROR_FILE_EXISTS = 80
ERROR_INVALID_NAME = 123
ERROR_ALREADY_EXISTS = 183


class _Win32Bindings:
    """惰性加载的 MoveFileExW / GetLastError 绑定。"""

    _instance = None

    def __init__(self) -> None:
        if os.name != "nt":
            raise NotImplementedError("win32 durable namespace helpers are only available on Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move = self._kernel32.MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move.restype = ctypes.c_int

    @classmethod
    def get(cls) -> "_Win32Bindings":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def move_file_ex_w(self, existing: str, replacement: str, flags: int) -> int:
        return self._kernel32.MoveFileExW(existing, replacement, flags)

    def get_last_error(self) -> int:
        return ctypes.get_last_error()


def _to_namespaced(path: str) -> str:
    """绝对路径的扩展长度命名空间拼写(``\\\\?\\`` 前缀)。

    裸盘符根(``E:\\``)保持原生拼写:系统拒绝盘符根的命名空间
    探测(报 EISDIR),且根本身没有长度问题。
    """
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute == os.path.splitdrive(absolute)[0] + os.sep:
        return absolute
    return f"\\\\?\\{absolute}"


def _errno_code(win32_code: int) -> str:
    """Win32 错误码 → Node 风格 errno 名。"""
    if win32_code in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
        return "ENOENT"
    if win32_code == ERROR_ACCESS_DENIED:
        return "EACCES"
    if win32_code == ERROR_NOT_SAME_DEVICE:
        return "EXDEV"
    if win32_code in (ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS):
        return "EEXIST"
    if win32_code == ERROR_INVALID_NAME:
        return "EINVAL"
    return "EIO"


def _win32_error(syscall: str, win32_code: int, path: str, dest: str) -> OSError:
    """带完整诊断上下文的 OSError(errno 映射 + Win32 原始码)。"""
    code = _errno_code(win32_code)
    error = OSError(win32_code, f"{syscall} {code} (Win32 {win32_code}): {path} -> {dest}")
    error.errno = win32_code
    error.win32Code = win32_code
    error.filename = path
    error.filename2 = dest
    return error


def _is_enoent(error: BaseException) -> bool:
    return getattr(error, "errno", None) == ERROR_FILE_NOT_FOUND or getattr(error, "errno", None) == ERROR_PATH_NOT_FOUND


def _is_eexist(error: BaseException) -> bool:
    return getattr(error, "errno", None) in (ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS)


def _assert_directory(path: str) -> bool:
    """path 存在且是目录返回 True;缺失返回 False;其他失败浮出。"""
    try:
        # 裸盘符根已足够短,且其扩展长度拼写被系统以 EISDIR 拒绝;
        # 后代保留命名空间供长路径探测。
        probe = path if path == os.path.splitdrive(path)[0] + os.sep else _to_namespaced(path)
        info = os.stat(probe)
        if stat_module.S_ISDIR(info.st_mode):
            return True
        error = OSError(f"path exists but is not a directory: {path}")
        error.errno = None
        error.filename = path
        raise error
    except OSError as error:
        if _is_enoent(error):
            return False
        raise


def publish_new_file_win32(existing: str, replacement: str) -> None:
    """以 Windows 写透重命名语义把 ``existing`` 发布到 ``replacement``。

    目标必须尚不存在;移动必须留在卷内(不设复制回退标志)。
    """
    bindings = _Win32Bindings.get()
    ok = bindings.move_file_ex_w(_to_namespaced(existing), _to_namespaced(replacement), MOVEFILE_WRITE_THROUGH)
    if ok == 0:
        raise _win32_error("MoveFileExW", bindings.get_last_error(), existing, replacement)


def ensure_durable_directory_win32(target: str) -> None:
    """创建 ``target`` 与缺失祖先,以耐久命名空间发布。

    每个缺失目录先以随机临时兄弟目录创建,再以
    MOVEFILE_WRITE_THROUGH 移到最终名;与另一创建者竞争时,只有
    验证胜者是目录才接受。
    """
    absolute = os.path.abspath(target)
    root = os.path.splitdrive(absolute)[0] + os.sep
    _assert_directory(root)

    segments = [part for part in absolute[len(root) :].replace("\\", "/").split("/") if part]
    current = root
    for segment in segments:
        next_path = os.path.join(current, segment)
        if not _assert_directory(next_path):
            _create_leaf_directory_win32(current, next_path)
        current = next_path


def _create_leaf_directory_win32(parent: str, target: str) -> None:
    """一个缺失目录:staging 兄弟 + 写透发布;EEXIST 竞争接受。"""
    # staging 组件与目标 basename 独立:合法的 255 字节目标组件
    # 不会把 mkdtemp 的兄弟名撑爆。
    staging = tempfile.mkdtemp(prefix=".dsh-mkdir-", dir=parent)
    try:
        publish_new_file_win32(staging, target)
    except OSError as error:
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
        if _is_eexist(error) and _assert_directory(target):
            return
        raise
