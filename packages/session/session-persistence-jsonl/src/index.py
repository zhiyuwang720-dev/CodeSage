"""JSONL 耐久会话持久化后端:每个会话一个 header + 连续事件的
追加式文件,编排交给 PersistenceCoordinator。它的零副作用定位器
在物化之前就返回绝对按会话日志路径。

DSH index.ts 的 Python 移植,按「零新依赖」决策砍掉 zstd 帧压缩:
物理编码固定纯 JSONL(``compression: 'none'``),压缩档位词汇与
扩展位保留 —— 见 format.py 与构造校验。Python 侧没有异步文件
原语,文件操作走同步 IO(小文件、写路径已被协调器逐 id 串行化,
阻塞代价可接受);扫描/解码逻辑与 DSH 逐行对应。
"""

from __future__ import annotations

import json
import os
import random
import shutil
import stat as stat_module

from core.session import SessionPreparation, SESSION_FORMAT_VERSION
from session.session_persistence import (
    DEFAULT_PREPARED_SESSION_CACHE_SIZE,
    DEFAULT_WRITE_BATCH_MAX_DELAY_MS,
    MAX_WRITE_BATCH_DELAY_MS,
    PersistenceBackend,
    PersistenceCoordinator,
    SessionFormatUnsupportedError,
    SessionInspection,
    SessionLocation,
    SessionPersistence,
    SessionPersistenceRevision,
    SessionPersistenceSnapshot,
    SessionRawArtifact,
    StoredPrefix,
)

from .format import (
    encode_segment,
    event_lines,
    log_path,
    log_suffix,
    parse_header_meta,
    project_dir,
    scan_log,
    session_dir,
    to_header_line,
)
from .win32 import ensure_durable_directory_win32, publish_new_file_win32

DEFAULT_PACK_CHUNKS = True
DEFAULT_COMPRESSION: str = "none"  # DSH 默认 zstd;本构建不带压缩,见模块 docstring

#: 撕裂尾修复令牌:协调器把它当不透明物;后端用它找回字节偏移
#: 与从撕裂物理尾恢复出的完整事件。
JsonlTornMarker = dict  # {"truncateTo": int, "recoveredEvents": list}


def file_revision(identity) -> SessionPersistenceRevision:
    """源限定修订号:dev:ino:size:mtime_ns:ctime_ns 五元组拼串。

    日志每次变化令牌都变;同一文件全量与轻量读共享同一修订号。
    """
    return SessionPersistenceRevision(
        ":".join(
            (
                str(identity.st_dev),
                str(identity.st_ino),
                str(identity.st_size),
                str(identity.st_mtime_ns),
                str(identity.st_ctime_ns),
            )
        )
    )


def _is_enoent(error: BaseException) -> bool:
    return isinstance(error, FileNotFoundError)


class JsonlSessionPersistence(SessionPersistence, PersistenceBackend):
    """JSONL 持久化后端。作为插件加载:经 Service 基类注册为
    ``ctx.sessionPersistence``,(经协调器)安装写路径监听者。
    """

    supportsRawArtifacts = True

    #: 后端标签,用于协调器诊断与 effect;遮蔽 Service.name。
    name = "session-persistence-jsonl"

    def __init__(self, ctx, config: dict) -> None:
        super().__init__(ctx)
        # 一次性 resolve:之后 process.cwd() 变化不能把一个后端拆到
        # 多个 root。
        self.root = os.path.abspath(config["root"])
        self.pack_chunks = config.get("packChunks", DEFAULT_PACK_CHUNKS)
        compression = config.get("compression", DEFAULT_COMPRESSION)
        if compression not in ("zstd", "none"):
            raise ValueError(f"compression must be 'zstd' or 'none', got {compression!r}")
        if compression == "zstd":
            raise ValueError(
                "compression 'zstd' is not bundled in this build (zero-dependency decision); "
                "use compression: 'none' or add pyzstd (requires Ask)"
            )
        self.compression = compression
        self._assert_usable_root()
        self.coordinator = PersistenceCoordinator(
            ctx,
            self,
            {
                "preparedSessionCacheSize": config.get(
                    "preparedSessionCacheSize", DEFAULT_PREPARED_SESSION_CACHE_SIZE
                ),
                "writeBatchMaxDelayMs": config.get(
                    "writeBatchMaxDelayMs", DEFAULT_WRITE_BATCH_MAX_DELAY_MS
                ),
            },
        )
        self._root_encoding_check = None

    # --- SessionPersistence 服务 API(委托协调器)---

    def locate(self, meta: dict) -> SessionLocation:
        """解析绝对目标路径,不碰文件系统。"""
        return SessionLocation("jsonl", log_path(self.root, meta.get("cwd"), meta["id"], self.compression))

    async def create(self, meta: dict) -> None:
        return await self.coordinator.create(meta)

    async def append(self, id: str, events: list) -> None:
        return await self.coordinator.append(id, events)

    async def prepare(self, id: str) -> SessionPreparation:
        return await self.coordinator.prepare(id)

    async def load(self, id: str) -> SessionInspection:
        return await self.coordinator.load(id)

    async def inspect(self, id: str) -> SessionInspection:
        return await self.coordinator.inspect(id)

    async def readFrom(self, id: str, fromSeq: int) -> dict:
        return await self.coordinator.readFrom(id, fromSeq)

    async def list(self) -> list:
        """列出有效唯一已存储会话的元数据(只读 header 行,不解析全日志)。"""
        return [artifact["header"] for artifact in await self._list_artifacts()]

    async def listSnapshots(self) -> list:
        """元数据 + 每个追加式日志的 stat 派生身份。"""
        snapshots = []
        for artifact in await self._list_artifacts():
            try:
                identity = os.stat(artifact["path"])
            except FileNotFoundError:
                continue  # 发现后被移除的工件不列入
            snapshots.append(
                SessionPersistenceSnapshot(artifact["header"], file_revision(identity))
            )
        return snapshots

    # --- PersistenceBackend 钩子(文件字节存储原语)---

    async def loadStored(self, id: str) -> StoredPrefix | None:
        """按 id 跨所有项目目录读存储前缀(cwd 未知时)。"""
        await self._ensure_root_encoding()
        path = await self._find_log(id)
        if path is None:
            return None
        return self._read_prefix(path, id)

    async def readStoredRevision(self, id: str) -> SessionPersistenceRevision | None:
        """stat 派生的修订号:不加载事件字节。"""
        await self._ensure_root_encoding()
        path = await self._find_log(id)
        if path is None:
            return None
        try:
            return file_revision(os.stat(path))
        except FileNotFoundError:
            return None

    async def readRaw(self, id: str) -> SessionRawArtifact | None:
        """逐字原样工件文本:后端写出的确切 JSONL 字节。

        打包行、键序与换行逐字节存活;撕裂尾省略(与其他读的
        已提交前缀语义一致)。返回带解析 header 的工件文本,或
        会话无工件时 None。
        """
        await self._ensure_root_encoding()
        path = await self._find_log(id)
        if path is None:
            return None
        buffer = self._read_stable_file(path)["buffer"]
        content = buffer.decode("utf-8")
        meta = parse_header_meta(content.split("\n", 1)[0])
        if meta is None or meta["id"] != id:
            raise ValueError(f'corrupt session log: invalid header line in "{path}"')
        # 逻辑工件名恒为 session.jsonl;物理后缀(.jsonl.zstd)只标记
        # 压缩。
        return SessionRawArtifact(meta, "session.jsonl", content)

    async def appendBatch(self, meta: dict, events: list, isMaterialized: bool) -> None:
        """耐久追加一批:惰性物化文件尚未存在时。"""
        await self._ensure_root_encoding()
        if isMaterialized:
            self._append_lines(meta, events)
        else:
            self._materialize(meta, events)

    async def commitRepair(
        self, meta: dict, tornMarker: JsonlTornMarker | None, closers: list
    ) -> None:
        """让崩溃修复耐久:截断撕裂尾、恢复其中的完整事件,再追加
        合成关闭器。两步 fsync —— 接缝处不要求原子。
        """
        if tornMarker is not None:
            self._repair(meta, tornMarker["truncateTo"])
        repaired_events = [*(tornMarker or {}).get("recoveredEvents", []), *closers]
        if len(repaired_events) > 0:
            self._append_lines(meta, repaired_events)

    # --- 读:稳定文件 / 前缀 / 列表 ---

    def _read_stable_file(self, path: str) -> dict:
        """修订稳定循环里读文件字节:写者在 stat 与 read 之间追加会
        产生撕裂的物理文件,stat 修订变化时重试。
        """
        while True:
            before = file_revision(os.stat(path))
            with open(path, "rb") as handle:
                buffer = handle.read()
            after = file_revision(os.stat(path))
            if before == after:
                return {"buffer": buffer, "revision": after}

    def _read_prefix(self, path: str, expected_id: str | None = None) -> StoredPrefix:
        """读存储前缀,把撕裂尾状态转成协调器可往返的不透明标记。"""
        result = self._read_stable_file(path)
        buffer, revision = result["buffer"], result["revision"]
        try:
            scan = scan_log(buffer)
            prefix = {
                "meta": scan["meta"],
                "events": scan["events"],
                **(
                    {"tornMarker": {"truncateTo": scan["committedBytes"], "recoveredEvents": []}}
                    if scan["committedBytes"] < len(buffer)
                    else {}
                ),
            }
        except SessionFormatUnsupportedError as error:
            # 解析期格式拒绝先于任何 SessionHeader,协调器的
            # locate 富化跑不了:把本次读取真正拒绝的工件附上。
            if getattr(error, "location", None) is None:
                raise SessionFormatUnsupportedError(f"{error} (raw log: {path})", SessionLocation("jsonl", path)) from None
            raise
        self._assert_stored_identity(path, prefix["meta"], expected_id)
        return StoredPrefix(prefix["meta"], prefix["events"], revision, prefix.get("tornMarker"))

    async def _list_artifacts(self) -> list:
        await self._ensure_root_encoding()
        artifacts = []
        ids = set()
        for project in await self._list_project_dirs():
            for dir_ in await self._list_session_dirs(project):
                opposite = os.path.join(dir_, f"session{log_suffix(self._opposite_compression())}")
                if self._exists(opposite):
                    raise self._encoding_mismatch(opposite)
                path = os.path.join(dir_, f"session{log_suffix(self.compression)}")
                if not self._exists(path):
                    continue
                # 只读 header:列表随会话数伸缩,不随日志总大小。
                first = self._read_first_line(path)
                if first is None:
                    continue  # 空/半写文件
                meta = parse_header_meta(first)
                if meta is None:
                    continue  # 不是会话 header
                self._assert_stored_identity(path, meta)
                if meta["id"] in ids:
                    raise ValueError(
                        f'duplicate JSONL session id "{meta["id"]}" appears in multiple project directories'
                    )
                ids.add(meta["id"])
                artifacts.append({"header": meta, "path": path})
        return artifacts

    # --- 物化 / 追加 / 修复(文件机制)---

    def _materialize(self, meta: dict, events: list) -> None:
        """原子写 header 行 + 首批事件(temp 写、fsync、发布)。"""
        project = project_dir(self.root, meta.get("cwd"))
        dir_ = session_dir(self.root, meta.get("cwd"), meta["id"])
        final_path = log_path(self.root, meta.get("cwd"), meta["id"], self.compression)
        self._reject_opposite_artifact(meta.get("cwd"), meta["id"])
        header = json.dumps(to_header_line(meta), ensure_ascii=False) + "\n"
        body = event_lines(events, self.pack_chunks) + "\n"
        content = header + body
        if os.name == "nt":
            self._materialize_win32(project, dir_, final_path, meta["id"], content)
        else:
            self._materialize_posix(project, dir_, final_path, meta["id"], content)

    def _materialize_posix(
        self, project: str, dir_: str, final_path: str, id: str, content: str
    ) -> None:
        """POSIX 发布:逐级建目录并 fsync,再 link()+unlink()。"""
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        self._sync_dir_posix(os.path.dirname(self.root))
        os.makedirs(project, mode=0o700, exist_ok=True)
        self._sync_dir_posix(self.root)
        os.makedirs(dir_, mode=0o700, exist_ok=True)
        self._sync_dir_posix(project)
        self._reject_existing_log(final_path, id)
        tmp = self._write_synced_temp_file(final_path, content)
        # 用 link()+unlink() 发布,不用 rename():link 在最终路径
        # 已存在时报 EEXIST,两个进程并发物化同一 id 不会互相覆盖。
        # rename() 会静默覆盖。
        linked = False
        try:
            os.link(tmp, final_path)
            linked = True
        finally:
            if not linked:
                os.remove(tmp)
        # link() 成功即发布完成;fsync 目录让新目录项经受断电:
        # 新链接在父目录元数据同步前不具崩溃耐久性。
        self._sync_dir_posix(dir_)
        # 尽力清理 temp:日志已发布且耐久,temp 硬链接删除失败
        # 绝不能拒绝这次追加。
        try:
            os.remove(tmp)
        except OSError:
            pass

    def _materialize_win32(
        self, project: str, dir_: str, final_path: str, id: str, content: str
    ) -> None:
        """Windows 发布:耐久目录命名空间 + 写透 MoveFileExW。"""
        ensure_durable_directory_win32(self.root)
        ensure_durable_directory_win32(project)
        ensure_durable_directory_win32(dir_)
        self._reject_existing_log(final_path, id)
        tmp = self._write_synced_temp_file(final_path, content)
        try:
            publish_new_file_win32(tmp, final_path)
        except OSError:
            os.remove(tmp)
            raise

    def _reject_existing_log(self, final_path: str, id: str) -> None:
        # 绝不发布覆盖已提交日志:物化是后端认为全新会话的第一次
        # 写。此处的文件意味着磁盘上有一个同 id 的不同会话 —— 响亮
        # 拒绝(createCore 已守卫 create 路径,这里是 TOCTOU 兜底)。
        if self._exists(final_path):
            raise ValueError(
                f'refusing to materialize "{id}": a log already exists on disk (load/resume it instead)'
            )

    def _write_synced_temp_file(self, final_path: str, content: str) -> str:
        tmp = f"{final_path}.{random.randbytes(6).hex()}.tmp"
        # Windows 的 os.open 默认文本模式(_O_TEXT)会把 \n 转成 \r\n;
        # O_BINARY 保证跨平台字节一致(POSIX 上 O_BINARY == 0)。
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return tmp

    @staticmethod
    def _sync_dir_posix(dir_: str) -> None:
        """fsync 一个目录,让刚创建的目录项崩溃耐久。"""
        fd = os.open(dir_, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _append_lines(self, meta: dict, events: list) -> None:
        """追加并 fsync 事件行。部分写或同步失败时恢复先前大小再
        重抛:未变的游标会重试本批次,留下部分字节会制造重复 seq。
        """
        content = event_lines(events, self.pack_chunks) + "\n"
        path = log_path(self.root, meta.get("cwd"), meta["id"], self.compression)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_BINARY)
        try:
            before = os.fstat(fd).st_size
            try:
                os.write(fd, content.encode("utf-8"))
                os.fsync(fd)
            except OSError as error:
                try:
                    self._rollback_append(path, before)
                except OSError as rollback_error:
                    raise OSError(f"failed to roll back append to {path!r}") from rollback_error
                raise error
        finally:
            os.close(fd)

    def _rollback_append(self, path: str, size: int) -> None:
        fd = os.open(path, os.O_RDWR | os.O_BINARY)
        try:
            os.ftruncate(fd, size)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _repair(self, meta: dict, offset: int) -> None:
        """把日志截断到 offset 字节并 fsync(丢弃崩溃尾)。"""
        path = log_path(self.root, meta.get("cwd"), meta["id"], self.compression)
        fd = os.open(path, os.O_RDWR | os.O_BINARY)
        try:
            os.ftruncate(fd, offset)
            os.fsync(fd)
        finally:
            os.close(fd)

    # --- 发现辅助 ---

    def _read_first_line(self, path: str) -> str | None:
        """不加载整个文件读第一个换行终止行。文件空或无完整首行
        返回 None;有界分块读让巨大日志只付出 header 读取的代价。
        """
        with open(path, "rb") as handle:
            chunks = []
            while True:
                chunk = handle.read(8192)
                if not chunk:
                    return None  # EOF 无换行 → 无完整行
                newline = chunk.find(b"\n")
                if newline != -1:
                    chunks.append(chunk[:newline])
                    return b"".join(chunks).decode("utf-8")
                chunks.append(chunk)

    async def _find_log(self, id: str) -> str | None:
        """跨每个项目目录找该 id 的唯一物理日志。"""
        matches = []
        for project in await self._list_project_dirs():
            self._reject_legacy_flat_artifact(project, id)
            dir_ = os.path.join(project, encode_segment(id))
            path = os.path.join(dir_, f"session{log_suffix(self.compression)}")
            opposite = os.path.join(dir_, f"session{log_suffix(self._opposite_compression())}")
            if self._exists(opposite):
                raise self._encoding_mismatch(opposite)
            if self._exists(path):
                matches.append(path)
        if len(matches) > 1:
            raise ValueError(
                f'duplicate JSONL session id "{id}" appears in multiple project directories'
            )
        return matches[0] if matches else None

    def _assert_usable_root(self) -> None:
        """要求已存在的配置 root 可读且是目录。"""
        try:
            info = os.stat(self.root)
        except FileNotFoundError:
            return  # 缺席 root 在首次物化时创建
        if not stat_module.S_ISDIR(info.st_mode):
            raise NotADirectoryError(f"configured session root is not a directory: {self.root}")

    def _assert_stored_identity(self, path: str, meta: dict, expected_id: str | None = None) -> None:
        """拒绝不识别所选物理日志的元数据。"""
        if expected_id is not None and meta.get("id") != expected_id:
            raise ValueError(
                f'corrupt session log "{path}": requested id "{expected_id}" does not match header id "{meta.get("id")}"'
            )
        try:
            expected_path = log_path(self.root, meta.get("cwd"), meta["id"], self.compression)
        except (ValueError, KeyError):
            raise ValueError(
                f'corrupt session log "{path}": header id cannot name a storage path'
            ) from None
        if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(expected_path)):
            # 大小写不敏感文件系统上的大小写别名被 normcase 吸收;
            # 真正指向不同文件的拼写必须拒绝。
            if os.path.exists(expected_path) and os.path.samefile(path, expected_path):
                return
            raise ValueError(
                f'corrupt session log "{path}": header id "{meta.get("id")}" and cwd identify "{expected_path}"'
            )

    async def _list_project_dirs(self) -> list:
        """配置 root 下的人类可读项目目录。"""
        try:
            return [
                os.path.join(self.root, entry.name)
                for entry in os.scandir(self.root)
                if entry.is_dir()
            ]
        except FileNotFoundError:
            return []  # 只有缺席 root 表示无会话;其他 I/O 失败浮出

    async def _list_session_dirs(self, project: str) -> list:
        """列出会话自有目录,拒绝已废弃的平铺文件布局。"""
        try:
            entries = list(os.scandir(project))
        except FileNotFoundError:
            return []
        legacy = next(
            (entry for entry in entries if entry.is_file() and (entry.name.endswith(".jsonl") or entry.name.endswith(".jsonl.zstd"))),
            None,
        )
        if legacy is not None:
            raise self._legacy_layout(os.path.join(project, legacy.name))
        return [os.path.join(project, entry.name) for entry in entries if entry.is_dir()]

    async def _ensure_root_encoding(self) -> None:
        """首次读前检查 root 是否已属于另一种物理编码(缓存一次性)。"""
        if self._root_encoding_check is None:
            self._root_encoding_check = await self._check_root_encoding()

    async def _check_root_encoding(self) -> None:
        for project in await self._list_project_dirs():
            for dir_ in await self._list_session_dirs(project):
                incompatible = os.path.join(dir_, f"session{log_suffix(self._opposite_compression())}")
                if self._exists(incompatible):
                    raise self._encoding_mismatch(incompatible)

    def _reject_legacy_flat_artifact(self, project: str, id: str) -> None:
        encoded = encode_segment(id)
        for compression in ("zstd", "none"):
            path = os.path.join(project, encoded + log_suffix(compression))
            if self._exists(path):
                raise self._legacy_layout(path)

    def _reject_opposite_artifact(self, cwd: str | None, id: str) -> None:
        path = log_path(self.root, cwd, id, self._opposite_compression())
        if self._exists(path):
            raise self._encoding_mismatch(path)

    def _opposite_compression(self) -> str:
        return "zstd" if self.compression == "none" else "none"

    def _encoding_mismatch(self, path: str) -> ValueError:
        return ValueError(
            f"session artifact {json.dumps(path)} uses {log_suffix(self._opposite_compression())}, "
            f"but this backend is configured for compression {json.dumps(self.compression)}; "
            "use a separate root or select the matching compression mode"
        )

    def _legacy_layout(self, path: str) -> ValueError:
        return ValueError(
            f"session artifact {json.dumps(path)} uses the unsupported flat-file layout; "
            "use a separate root or move it into a project/session directory before loading"
        )

    def _exists(self, path: str) -> bool:
        """打开探测存在性;只有 ENOENT 意味着缺席,权限/I/O 错误必须
        浮出,而不是让加载或碰撞检查在虚假缺席下继续。Windows 对
        ``常规文件/子路径`` 报 ENOENT 而非 ENOTDIR:先验证直接父路径
        ,让被阻塞的会话目录仍是存储故障。
        """
        try:
            fd = os.open(path, os.O_RDONLY)
            os.close(fd)
            return True
        except FileNotFoundError:
            self._assert_log_parent_allows_absence(path)
            return False
        except OSError as error:
            if getattr(error, "errno", None) == 21:  # EISDIR:目录当文件开
                return False
            raise

    def _assert_log_parent_allows_absence(self, path: str) -> None:
        """Windows 修复:ENOENT 也可能是父路径是文件 —— 那仍是故障。"""
        parent = os.path.dirname(path)
        try:
            info = os.stat(parent)
        except FileNotFoundError:
            return
        if not stat_module.S_ISDIR(info.st_mode):
            raise NotADirectoryError(f"ENOTDIR: parent path exists but is not a directory: {parent}")


# 压缩档位词汇(格式扩展位,见 format.py):本构建只走 'none'。
__all__ = [
    "DEFAULT_COMPRESSION",
    "DEFAULT_PACK_CHUNKS",
    "JsonlSessionPersistence",
    "JsonlTornMarker",
    "file_revision",
]
