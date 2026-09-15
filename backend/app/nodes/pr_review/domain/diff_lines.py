"""统一 diff 的新增行解析(阶段 02): 行号以 head 分支为准(§7 评论行号)。

供规则引擎(只审新增行)与综合层(评论必须落在新增行, spec §2 原则③)共用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
_FILE_RE = re.compile(r"^diff --git a/(?P<old>.+?) b/(?P<new>.+)$")
_ADDED_RE = re.compile(r"^\+(?P<content>.*)$")


@dataclass(frozen=True)
class AddedLine:
    path: str
    line: int  # head 分支行号
    content: str


def parse_added_lines(diff_text: str) -> list[AddedLine]:
    """统一 diff → 新增行列表(忽略二进制文件块)。"""
    added: list[AddedLine] = []
    current_path: str | None = None
    head_line = 0
    in_binary = False
    for raw in diff_text.splitlines():
        file_match = _FILE_RE.match(raw)
        if file_match:
            current_path = file_match["new"]
            in_binary = False
            continue
        if current_path is None:
            continue
        if raw.startswith("GIT binary patch") or raw.startswith("Binary files"):
            in_binary = True
            continue
        if raw.startswith(("diff --git", "index ", "--- ", "+++", "new file mode", "deleted file mode", "similarity index", "rename from", "rename to", "old mode", "new mode")):
            continue
        if in_binary:
            continue
        hunk = _HUNK_RE.match(raw)
        if hunk:
            head_line = int(hunk["start"])
            continue
        if raw.startswith("-"):
            continue
        added_match = _ADDED_RE.match(raw)
        if added_match and raw != "+++":
            added.append(
                AddedLine(path=current_path, line=head_line, content=added_match["content"])
            )
            head_line += 1
        elif raw.startswith(" "):
            head_line += 1
        # "\ No newline at end of file" 等杂项行忽略
    return added


def added_line_index(diff_text: str) -> dict[str, set[int]]:
    """file → head 新增行号集合(综合层落行校验用)。"""
    index: dict[str, set[int]] = {}
    for item in parse_added_lines(diff_text):
        index.setdefault(item.path, set()).add(item.line)
    return index


def changed_files(diff_text: str) -> list[str]:
    return list(added_line_index(diff_text).keys())
