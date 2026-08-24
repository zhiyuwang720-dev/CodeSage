"""win32 耐久命名空间单测(ctypes FFI 封装)。

覆盖 DSH win32.spec.ts 断言面:盘符根原生探测 + 后代命名空间、
写透发布、发布失败 errno 映射、staging 兄弟建目录与 EEXIST 竞争
容忍、staging 命名对最长目标组件的有效性、非目录组件的拒绝。
非 Windows 平台跳过(绑定层本身抛 NotImplementedError)。
"""

import os
import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[1]
_PACKAGES = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_PACKAGES))

pytestmark = pytest.mark.skipif(os.name != "nt", reason="win32 durable namespace helpers are Windows-only")

from session.session_persistence_jsonl.src.win32 import (  # noqa: E402
    ERROR_ACCESS_DENIED,
    ERROR_ALREADY_EXISTS,
    ERROR_FILE_EXISTS,
    _Win32Bindings,
    _assert_directory,
    _is_enoent,
    _to_namespaced,
    ensure_durable_directory_win32,
    publish_new_file_win32,
)


def test_drive_root_probes_native_descendants_namespaced():
    # 裸盘符根保持原生拼写;后代进入 \\?\ 命名空间(长路径)
    root = os.path.splitdrive(os.path.abspath("."))[0] + os.sep
    assert _to_namespaced(root) == root
    child = _to_namespaced(os.path.join(root, "x"))
    assert child.startswith("\\\\?\\")
    assert child.endswith("x")


def test_publishes_new_file_with_write_through(tmp_path):
    source = tmp_path / "src.tmp"
    source.write_bytes(b"payload")
    target = tmp_path / "target"
    publish_new_file_win32(str(source), str(target))
    assert target.read_bytes() == b"payload"
    assert not source.exists()  # 移动语义


def test_publish_failure_maps_to_errno_codes(tmp_path):
    # 目标已存在 → EEXIST 族(Win32 80/183)
    source = tmp_path / "s.tmp"
    source.write_bytes(b"x")
    target = tmp_path / "t"
    target.write_bytes(b"y")
    with pytest.raises(OSError) as info:
        publish_new_file_win32(str(source), str(target))
    assert info.value.errno in (ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS)
    # 缺失源 → ENOENT 族(Win32 2/3)
    with pytest.raises(OSError) as info:
        publish_new_file_win32(str(tmp_path / "absent"), str(tmp_path / "t2"))
    assert _is_enoent(info.value)


def test_creates_missing_directories_tolerates_race(tmp_path):
    target = tmp_path / "a" / "b" / "c"
    ensure_durable_directory_win32(str(target))
    assert target.is_dir()
    # 目标已存在:直接通过(幂等)
    ensure_durable_directory_win32(str(target))
    assert target.is_dir()
    # 逐层创建
    nested = tmp_path / "x" / "y" / "z" / "w"
    ensure_durable_directory_win32(str(nested))
    assert nested.is_dir()


def test_staging_name_valid_for_max_length_component(tmp_path):
    # 255 字节目标组件:staging 兄弟名(前缀 .dsh-mkdir-)不因此失效
    target = tmp_path / ("m" * 255)
    ensure_durable_directory_win32(str(target))
    assert _assert_directory(str(target))  # 命名空间探测(普通路径超 260 限制)


def test_surfaces_non_race_directory_publication_failures(tmp_path):
    # 把文件当目录创建:发布链暴露失败而非静默
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")
    child = blocker / "child"
    with pytest.raises(OSError):
        ensure_durable_directory_win32(str(child))


def test_rejects_non_directory_component(tmp_path):
    # 已存在但非目录的组件:明确拒绝而非当作缺失
    blocker = tmp_path / "file"
    blocker.write_bytes(b"x")
    with pytest.raises(OSError) as info:
        _assert_directory(str(blocker))
    assert info.value.filename == str(blocker)  # 错误点名该路径


def test_bindings_lazy_and_shared():
    bindings = _Win32Bindings.get()
    assert bindings is _Win32Bindings.get()  # 单例
    assert bindings.move_file_ex_w is not None
