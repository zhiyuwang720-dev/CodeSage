"""落盘格式辅助:路径净化、目录布局、header 行序列化、撕裂修复偏移。

DSH format.ts 的 Python 移植。SessionId 是未校验的品牌字符串,必须
先编码才能用作路径 —— 无路径穿越、无碰撞。本模块同时承载
每项目/每会话的目录布局、header 行(反)序列化,以及截断修复的
字节偏移计算。

物理编码词汇保留 DSH 的 ``'zstd' | 'none'`` 双档(压缩开关),
但本移植按「零新依赖」决策只实现纯 JSONL:``'zstd'`` 档保留后缀
与选择逻辑,写入端永远走 ``'none'``(见 index.py 构造校验)。
物理层与逻辑层分离:读者对布局无感(scan_log 永远解码记录行),
压缩开关只改变新写入的字节。
"""

from __future__ import annotations

import json
import math
import re

from core.session import SESSION_FORMAT_VERSION, decode_storage_record, pack_chunk_runs

from session.session_persistence import SessionFormatUnsupportedError, sessionFormatVersionRefusal

# TS 的 JsonlCompression:物理编码选择。
JsonlCompression = str  # 'zstd' | 'none'

_COMPRESSION_ZSTD = "zstd"
_COMPRESSION_NONE = "none"

_MAX_SAFE_INTEGER = 2**53 - 1

_SAFE_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]")


def _code_units(raw: str):
    """按 UTF-16 code unit 迭代(DSH charCodeAt 语义)。

    Python 的 str 迭代以 code point 为单位;对来自 UTF-16 域
    (含 lone surrogate)的字符串,code point 与 code unit 一一
    对应,这里显式走 surrogate 域保证一致性。
    """
    encoded = raw.encode("utf-16-le", errors="surrogatepass")
    for i in range(0, len(encoded), 2):
        yield encoded[i] | (encoded[i + 1] << 8)


def log_suffix(compression: str) -> str:
    """一种物理编码的工件后缀:``.jsonl.zstd`` 或 ``.jsonl``。"""
    return ".jsonl.zstd" if compression == _COMPRESSION_ZSTD else ".jsonl"


# --- header 行(反)序列化 ---

# HeaderLine:会话工件的第一条 JSONL 记录 —— 带 ``session`` 标签的
# 不可变 SessionHeader,读者借此与事件行区分。
HEADER_LINE_FIELDS = ("version", "id", "createdAt", "cwd", "parentSession", "seedLength", "origin", "delegationDepth", "agentPreset")


def to_header_line(header: dict) -> dict:
    """从 SessionHeader 构建 ``type: 'session'`` 标签的行对象。

    可选的 header 字段缺席时省略(绝不为 None);delegationDepth
    缺省补 0。
    """
    line = {
        "type": "session",
        "version": header["version"],
        "id": header["id"],
        "createdAt": header["createdAt"],
    }
    for field in ("cwd", "parentSession", "seedLength", "origin", "agentPreset"):
        if field in header:
            line[field] = header[field]
    line["delegationDepth"] = header.get("delegationDepth", 0)
    return line


def from_header_line(line: dict) -> dict:
    """把 shape 校验过的首行解析回 SessionHeader(缺席可选字段省略)。

    拒绝已退役的策略基线字段:它们的存在说明日志由旧构建写出,
    而其字段解释在本构建中已不可信。
    """
    if "sandboxMode" in line or "approvalPolicy" in line:
        raise ValueError("session header uses retired policy baseline fields")
    header = {
        "version": line["version"],
        "id": line["id"],
        "createdAt": line["createdAt"],
    }
    for field in ("cwd", "parentSession", "seedLength", "origin", "agentPreset"):
        if field in line:
            header[field] = line[field]
    header["delegationDepth"] = line["delegationDepth"]
    return header


def _safe_nonneg(value) -> int | None:
    """TS Number.isSafeInteger + >= 0 + 非 -0(接受 1 与 1.0,拒绝 1.5/-1/-0.0)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        if value == 0 and math.copysign(1.0, value) < 0:
            return None
        value = int(value)
    if 0 <= value <= _MAX_SAFE_INTEGER:
        return value
    return None


def is_header_line(value) -> bool:
    """类型守卫:解析出的首行是良构的会话 header。"""
    if not isinstance(value, dict):
        return False
    if value.get("type") != "session":
        return False
    if not isinstance(value.get("version"), int) or isinstance(value.get("version"), bool):
        return False
    if not isinstance(value.get("id"), str):
        return False
    if _safe_nonneg(value.get("createdAt")) is None:
        return False
    if _safe_nonneg(value.get("delegationDepth")) is None:
        return False
    origin = value.get("origin")
    if origin is not None and origin != "subagent":
        return False
    agent_preset = value.get("agentPreset")
    if agent_preset is not None and not isinstance(agent_preset, str):
        return False
    return True


# --- 路径净化与布局 ---


def encode_segment(raw: str) -> str:
    """把一个任意字符串编码为单一安全路径段,对全部 JS(UTF-16)
    字符串(含 lone surrogate)单射。

    SessionId 是未校验的品牌字符串,必须先中和 ``../``、绝对路径、
    NUL 与分隔符才能碰文件系统。安全 code unit 保持字面;其余
    (含 ``~``)转义为 ``~XXXX``。以 code unit 为单位保留 lone
    surrogate;``.`` / ``..`` 特判,防止本来安全的整段被解释为
    路径穿越。
    """
    if len(raw) == 0:
        raise ValueError("cannot encode an empty path segment")
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out = []
    for code in _code_units(raw):
        ch = chr(code)
        if ch != "~" and _SAFE_SEGMENT_RE.match(ch):
            out.append(ch)
        else:
            out.append(f"~{code:04X}")
    return "".join(out)


def project_key(cwd: str) -> str:
    """为项目路径构建可读目录键。

    文件系统分隔符与盘符分隔符变 ``-``;不安全 code unit 用与
    会话 id 相同的 ``~XXXX`` 转义。键受文件系统组件长度限制,
    截断到 251 字符。分隔符替换与截断有意有损 —— 人类可导航的
    项目目录惯例。
    """
    if len(cwd) == 0:
        raise ValueError("cannot encode an empty project path")
    readable = []
    separator_run = False
    for code in _code_units(cwd):
        ch = chr(code)
        if ch in "/\\:":
            if not separator_run:
                readable.append("-")
            separator_run = True
        elif ch != "~" and _SAFE_SEGMENT_RE.match(ch):
            readable.append(ch)
            separator_run = False
        else:
            readable.append(f"~{code:04X}")
            separator_run = False
    slug = "".join(readable).lstrip("-") or "root"
    return f"--{slug[:251]}--"


def project_dir(root: str, cwd: str | None) -> str:
    """配置 root 下的人类可导航项目目录(cwd 缺席选 ``_no-cwd``)。"""
    if cwd is None:
        return _join(root, "_no-cwd")
    return _join(root, project_key(cwd))


def session_dir(root: str, cwd: str | None, id: str) -> str:
    """一个会话独占的目录(未来会话本地工件的落脚点)。"""
    return _join(project_dir(root, cwd), encode_segment(id))


def log_path(root: str, cwd: str | None, id: str, compression: str) -> str:
    """一个会话的追加式事件日志文件路径。"""
    return _join(session_dir(root, cwd, id), f"session{log_suffix(compression)}")


def _join(*parts: str) -> str:
    """路径拼接:统一走 os.path(win32 用反斜杠,与其他包一致)。"""
    from os.path import join

    return join(*parts)


# --- 记录行序列化 ---


def event_lines(events: list, pack_chunks: bool) -> str:
    """把一个事件批次序列化为 JSONL 行(无尾部换行)。

    ``pack_chunks`` 开启时,增量块连续段打包为 ``text-chunks`` /
    ``reasoning-chunks`` / ``tool-call-chunks`` 存储行;关闭时一个
    事件一行,与未打包布局字节一致。读取对布局无感(scan_log
    永远解码记录行),开关只改变新写入的字节。
    """
    records = pack_chunk_runs(events) if pack_chunks else events
    return "\n".join(json.dumps(record, ensure_ascii=False) for record in records)


# --- 扫描:header 解析 + 完整/撕裂前缀 ---


def refuse_foreign_format_version(parsed) -> None:
    """在验证当前 header 形状或解码任何事件行之前,拒绝携带本构建
    读不了的格式版本的 header:未来格式不必满足今天的结构检查,其
    使用者必须看到「升级 harness」,而不是「会话日志损坏」。
    """
    if not isinstance(parsed, dict):
        return
    version = parsed.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version == SESSION_FORMAT_VERSION:
        return
    id = parsed.get("id")
    raise SessionFormatUnsupportedError(
        sessionFormatVersionRefusal(id if isinstance(id, str) else str(id), version)
    )


def parse_header_record(record: bytes) -> dict:
    """解析一条完整 header 记录(独立于事件行提供)。

    record 必须非空、以换行结尾且换行是最后一个字节 —— 即恰好
    一条完整记录。
    """
    if len(record) == 0 or record[-1] != 0x0A or record.find(b"\n") != len(record) - 1:
        raise ValueError("empty or header-less session log")
    try:
        parsed = json.loads(record[:-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("corrupt session log: header line is not valid JSON") from None
    refuse_foreign_format_version(parsed)
    if not is_header_line(parsed):
        raise ValueError("corrupt session log: first line is not a session header")
    return from_header_line(parsed)


class SessionLogScanner:
    """增量扫描独立 header 记录之后的完整 JSONL 事件记录。

    换行搜索与字节偏移留在原始 buffer 上;只有完整记录解码为
    UTF-8。跨写入的碎片被拷贝 —— 解码器可能在 write() 返回后
    复用其输出 buffer。
    """

    def __init__(self, header_record: bytes) -> None:
        self._meta = parse_header_record(header_record)
        self._events: list = []
        self._fragments: list[bytes] = []
        self._fragment_bytes = 0
        self._input_bytes = len(header_record)
        self._committed_bytes = len(header_record)
        self._event_line = 0
        self._issue: ValueError | None = None
        self._finished = False

    @property
    def meta(self) -> dict:
        return self._meta

    @property
    def events(self) -> list:
        return self._events

    def write(self, chunk: bytes) -> None:
        """消费下一块原始明文,只保留未完成的最后一条记录。"""
        if self._finished:
            raise ValueError("cannot write to a finished session log scanner")
        chunk_start = self._input_bytes
        self._input_bytes += len(chunk)
        line_start = 0
        while True:
            newline = chunk.find(b"\n", line_start)
            if newline == -1:
                break
            fragment = chunk[line_start:newline]
            if self._fragments:
                if len(fragment) > 0:
                    self._fragments.append(fragment)
                line = b"".join(self._fragments)
                self._fragments = []
                self._fragment_bytes = 0
            else:
                line = fragment
            self._consume_event_line(line, chunk_start + newline + 1)
            line_start = newline + 1
        if line_start < len(chunk):
            fragment = bytes(chunk[line_start:])
            self._fragments.append(fragment)
            self._fragment_bytes += len(fragment)

    def checkpoint(self) -> dict:
        """在追加可恢复的撕裂帧前缀之前快照进度。

        返回字节、已提交前缀与展开事件游标三个档位。
        """
        return {
            "inputBytes": self._input_bytes,
            "committedBytes": self._committed_bytes,
            "eventCount": len(self._events),
        }

    def finish(self) -> dict:
        """结束扫描,把无换行的最后一条记录当作撕裂尾忽略。"""
        self._finished = True
        return {
            "meta": self._meta,
            "events": self._events,
            "committedBytes": self._committed_bytes,
        }

    def _consume_event_line(self, line: bytes, end_byte: int) -> None:
        """解码一条完整事件行并更新连续前缀。"""
        self._event_line += 1
        try:
            decoded = decode_storage_record(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._issue = self._issue or ValueError(
                f"corrupt session log: unparsable committed event at line {self._event_line}"
            )
            return

        if self._issue is not None:
            # 已提交前缀里出现损坏:它是否在 turn/end 之前决定了
            # 语义 —— 未关回合的日志绝不能静默当完整历史读。
            if any(event.get("type") == "turn/end" for event in decoded):
                raise self._issue
            return

        row_start = len(self._events)
        for event in decoded:
            if event["seq"] != len(self._events):
                expected = len(self._events)
                del self._events[row_start:]
                self._issue = ValueError(
                    f"corrupt session log: seq gap in committed region at line {self._event_line} "
                    f"(expected {expected}, got {event['seq']})"
                )
                if any(candidate.get("type") == "turn/end" for candidate in decoded):
                    raise self._issue
                return
            self._events.append(event)
        self._committed_bytes = end_byte


def scan_log(buffer: bytes) -> dict:
    """把完整或撕裂的 JSONL buffer 解析为保留的事件前缀。

    兼容包装:header 记录独立提供,事件行委托给
    SessionLogScanner。

    返回 header、保留的事件前缀,以及可安全追加的字节偏移。
    """
    header_end = buffer.find(b"\n")
    if header_end == -1:
        raise ValueError("empty or header-less session log")
    scanner = SessionLogScanner(buffer[: header_end + 1])
    scanner.write(buffer[header_end + 1 :])
    return scanner.finish()


def parse_header_meta(first_line: str) -> dict | None:
    """只解析一个日志的 header 行。

    list() 用它读会话元数据而不解析整个日志:会话选择器随会话
    数伸缩,不随所有对话的总大小伸缩。首行缺失或不是良构 header
    时返回 None。
    """
    try:
        parsed = json.loads(first_line)
    except json.JSONDecodeError:
        return None
    if not is_header_line(parsed):
        return None
    return from_header_line(parsed)
