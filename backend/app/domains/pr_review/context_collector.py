"""context_collector(阶段 01 核心, §3.3): 审查运行前的确定性上下文收集。

四维度: git 历史 / 相关文件(diff 引用分析) / CI 状态 / 用户注入。
实现要点(§3.3):
1. 确定性优先 —— 相关文件用正则+文件存在性解析, 不依赖 LLM, 可复现可缓存;
2. 预算控制 —— 按引用强度排序, 总字节数受预算约束;
3. 产物落盘 —— ReviewContext 写入 .auditai/context/<pr_key>.json;
4. diff-only 模式跳过自动收集, git_history/related_files 为空数组。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .git_providers import run_git
from .models import GitCommitInfo, ImportedPr, RelatedFile, ReviewContext
from .paths import context_path

# ── 预算与扫描上限 ────────────────────────────────────────────────
DEFAULT_FILE_BUDGET_BYTES = 60_000  # 相关文件总字节预算(≈15k token, 阶段02承接压缩)
DEFAULT_MAX_RELATED_FILES = 20
MAX_SCAN_FILES = 2000  # caller 扫描的文件数上限(防超大仓库失控)
MAX_SCAN_FILE_BYTES = 512_000  # 单文件参与扫描的大小上限
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".idea", ".vscode"}

# ── diff 解析 ────────────────────────────────────────────────────
_DIFF_FILE_RE = re.compile(r"^diff --git a/(?P<old>.+?) b/(?P<new>.+)$", re.M)
_BINARY_RE = re.compile(r"^GIT binary patch", re.M)
_IMPORT_LINE_RE = re.compile(r"^\+(?!\+\+)\s*(?P<stmt>.+)$")

_PY_IMPORT_RE = re.compile(
    r"^(?:from\s+(?P<frm>[\w.]+)\s+import|import\s+(?P<imp>[\w.,\s]+?))(?:\s+as\s+\w+)?$"
)
_JS_IMPORT_RE = re.compile(r"""^(?:import\s.+?from|require)\s*\(?\s*['"](?P<mod>[^'"]+)['"]""")
_REL_IMPORT_RE = re.compile(r"^from\s+(?P<dot>\.+)(?P<rest>[\w.]*)\s+import")

# 常见源码扩展(deterministic 解析的目标语言: Python + JS/TS 为主)
_CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".java"}


def extract_changed_files(diff_text: str) -> list[str]:
    """diff → 变更文件列表; 二进制文件只列名不展开(§7)。"""
    files: list[str] = []
    seen: set[str] = set()
    for m in _DIFF_FILE_RE.finditer(diff_text):
        path = m["new"]
        if path in seen or path == "/dev/null":
            continue
        seen.add(path)
        files.append(path)
    return files


def _extract_added_import_lines(diff_text: str) -> list[str]:
    """diff 新增行里的 import/require 语句(§3.3: diff 中 import 的目标文件)。"""
    stmts: list[str] = []
    for line in diff_text.splitlines():
        m = _IMPORT_LINE_RE.match(line)
        if m:
            stmt = m["stmt"].strip()
            if stmt.startswith(("import ", "from ", "require", "import(")):
                stmts.append(stmt)
    return stmts


def _module_stmt_to_target(stmt: str) -> str | None:
    """import 语句 → 模块名字符串; 相对导入返回原点前缀。"""
    m = _REL_IMPORT_RE.match(stmt)
    if m:
        return m["dot"] + (m["rest"] or "")
    m = _PY_IMPORT_RE.match(stmt)
    if m:
        return m["frm"] or m["imp"]
    m = _JS_IMPORT_RE.match(stmt)
    if m:
        return m["mod"]
    return None


def _module_to_relative_paths(module: str, importer_dir: str | None) -> list[str]:
    """模块名 → 仓库相对路径候选(Python 点路径 + JS 相对路径启发式)。"""
    candidates: list[str] = []
    if module.startswith("."):
        rel = module.lstrip(".")
        depth = len(module) - len(rel) - 1  # '.' 个数
        parts = [p for p in rel.split(".") if p] if rel else []
        up = "../" * max(depth - 1, 0)
        base = up + "/".join(parts)
        for ext in _CODE_EXTS:
            candidates.append(f"{base}{ext}")
        candidates.append(f"{base}/__init__.py")
        candidates.append(f"{base}/index.ts")
        return candidates
    parts = [p for p in module.replace("/", ".").split(".") if p and p != ""] if module else []
    if not parts:
        return []
    dotted = "/".join(parts)
    for ext in _CODE_EXTS:
        candidates.append(f"{dotted}{ext}")
        candidates.append(f"src/{dotted}{ext}")
    candidates.append(f"{dotted}/__init__.py")
    if importer_dir:
        candidates.append(str(Path(importer_dir) / f"{parts[-1]}.py"))
    return candidates


def _resolve_existing(repo_dir_path: Path, rel_paths: list[str]) -> list[str]:
    found: list[str] = []
    for rel in rel_paths:
        p = (repo_dir_path / rel).resolve()
        try:
            p.relative_to(repo_dir_path.resolve())
        except ValueError:
            continue
        if p.is_file():
            found.append(p.relative_to(repo_dir_path.resolve()).as_posix())
    return found


def _iter_code_files(repo_dir_path: Path, limit: int = MAX_SCAN_FILES):
    count = 0
    for root, dirs, files in os.walk(repo_dir_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if count >= limit:
                return
            p = Path(root) / name
            if p.suffix.lower() not in _CODE_EXTS:
                continue
            try:
                if p.stat().st_size > MAX_SCAN_FILE_BYTES:
                    continue
            except OSError:
                continue
            count += 1
            yield p


def _find_callers(repo_dir_path: Path, modules: list[str], changed: list[str]) -> list[str]:
    """扫描仓库找 import 了被改模块的文件(调用方, §3.3 维度②)。"""
    if not modules:
        return []
    module_set = {m for m in modules if m}
    import_patterns = []
    for m in module_set:
        if m.startswith("."):
            continue
        escaped = re.escape(m)
        import_patterns.extend(
            [
                re.compile(rf"^\s*(?:from\s+{escaped}(?:\.|\s)|import\s+{escaped}(?:\s|,|$|\.))", re.M),
                re.compile(rf"""['"]{escaped}['"]"""),
            ]
        )
    changed_set = set(changed)
    callers: list[str] = []
    for p in _iter_code_files(repo_dir_path):
        rel = p.relative_to(repo_dir_path.resolve()).as_posix()
        if rel in changed_set:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(pat.search(text) for pat in import_patterns):
            callers.append(rel)
    return callers


def _find_test_files(changed: list[str]) -> list[str]:
    """测试文件启发式: test_<stem>.py / <stem>_test.py / tests/ 下同名。"""
    tests: list[str] = []
    for path in changed:
        p = Path(path)
        stem, suffix = p.stem, p.suffix
        if stem.startswith("test_") or stem.endswith("_test"):
            continue
        for cand in (
            p.with_name(f"test_{stem}{suffix}"),
            p.with_name(f"{stem}_test{suffix}"),
            p.parent / "tests" / f"test_{stem}{suffix}",
            Path("tests") / p.parent / f"test_{stem}{suffix}",
            Path("tests") / f"test_{stem}{suffix}",
        ):
            if str(cand.as_posix()) not in tests:
                tests.append(cand.as_posix())
    return tests


def collect_related_files(
    repo_dir_path,
    diff_text: str,
    budget_bytes: int = DEFAULT_FILE_BUDGET_BYTES,
    max_files: int = DEFAULT_MAX_RELATED_FILES,
) -> list[RelatedFile]:
    """确定性相关文件提取: import 解析(3) > 调用方(2) > 测试文件(1),
    按引用强度排序、累计字节数受预算约束, 超预算截断不崩溃(§3.3.2/§6)。"""
    if repo_dir_path is None:
        return []
    repo_dir_path = Path(repo_dir_path)
    changed = extract_changed_files(diff_text)
    if not changed:
        return []

    # ① diff import 解析(相对导入以 importer 所在目录为基准)
    import_targets: set[str] = set()
    importer_dirs = {str(Path(m["new"]).parent) for m in _DIFF_FILE_RE.finditer(diff_text)}
    for importer_dir in importer_dirs:
        for stmt in _extract_added_import_lines(diff_text):
            module = _module_stmt_to_target(stmt)
            if not module:
                continue
            import_targets.update(
                _resolve_existing(repo_dir_path, _module_to_relative_paths(module, importer_dir))
            )
    import_files = sorted(import_targets - set(changed))

    # ③ 测试文件(存在性校验)
    test_files = [t for t in _find_test_files(changed) if (repo_dir_path / t).is_file() and t not in changed]

    # ② 调用方(§3.3②: 被修改符号的调用方 —— 从被改文件推导模块名, 叠加 diff 新增 import 的目标模块)
    modules: set[str] = set()
    for path in changed:
        p = Path(path)
        if p.suffix in {".py", ".ts", ".tsx", ".js", ".jsx"}:
            modules.add(p.stem)
            dotted = ".".join(p.with_suffix("").parts)
            if len(p.parts) > 1:
                modules.add(dotted)
    for stmt in _extract_added_import_lines(diff_text):
        module = _module_stmt_to_target(stmt)
        if module and not module.startswith("."):
            modules.add(module)
    caller_files = [c for c in _find_callers(repo_dir_path, sorted(modules), changed) if c not in import_files]

    ranked: list[RelatedFile] = []
    seen_paths: set[str] = set()
    # 测试文件维度优先标注(即使它同时也是调用方, spec §3.3 维度③)
    for path, reason, strength in (
        *[(t, "test", 1) for t in test_files],
        *[(c, "caller", 2) for c in caller_files],
        *[(f, "import", 3) for f in import_files],
    ):
        if path in seen_paths:
            continue
        seen_paths.add(path)
        fp = repo_dir_path / path
        try:
            size = fp.stat().st_size
        except OSError:
            size = 0
        ranked.append(RelatedFile(path=path, reason=reason, strength=strength, size_bytes=size))

    ranked.sort(key=lambda r: (-r.strength, r.path))
    selected: list[RelatedFile] = []
    used = 0
    content_budget = budget_bytes
    for r in ranked:
        if len(selected) >= max_files or used + r.size_bytes > budget_bytes and selected:
            break
        used += r.size_bytes
        fp = repo_dir_path / r.path
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if len(text.encode("utf-8")) <= content_budget:
            r.content = text
            content_budget -= len(text.encode("utf-8"))
        selected.append(r)
    return selected


def collect_git_history(
    repo_dir_path, base_sha: str | None, head_sha: str | None, max_commits: int = 50
) -> list[GitCommitInfo]:
    """git 历史(§3.3 维度一): base..head 区间提交(去 \x1f 解析)。"""
    if not base_sha or not head_sha:
        return []
    out = run_git(
        repo_dir_path,
        "log",
        "--pretty=format:%H%x1f%an%x1f%s%x1f%P",
        f"{base_sha}..{head_sha}",
        check=False,
    )
    commits: list[GitCommitInfo] = []
    for line in out.splitlines():
        if "\x1f" not in line:
            continue
        sha, author, subject, parents = (line.split("\x1f") + [""])[:4]
        commits.append(
            GitCommitInfo(sha=sha, author=author, message=subject, is_merge=parents.strip().count(" ") > 0)
        )
        if len(commits) >= max_commits:
            break
    return commits


def collect_ci_status(provider, head_sha: str | None) -> dict | None:
    """CI 状态(§3.3 维度三, 可选): 不可用返回 None 不阻塞(§7)。"""
    try:
        return provider.get_ci_status(head_sha)
    except Exception:
        return None


def build_review_context(
    imported: ImportedPr,
    *,
    provider=None,
    user_context: str | None = None,
    command: str = "review",
    options: dict | None = None,
    collect_auto: bool = True,
) -> ReviewContext:
    """组装 ReviewContext 并落盘(§3.3.3); diff-only 模式跳过自动收集。"""
    repo_dir_path = Path(imported.repo_dir) if imported.repo_dir else None
    if imported.diff_only or repo_dir_path is None or not repo_dir_path.exists():
        ctx = ReviewContext(
            repo=imported.repo,
            pr_number=imported.pr_number,
            base_sha=imported.base_sha,
            head_sha=imported.head_sha,
            diff_text=imported.diff_text,
            diff_only=True,
            command=command,
            options=options or {},
            user_context=user_context,
            pr_key=imported.pr_key,
        )
    else:
        budget = int((options or {}).get("file_budget_bytes", DEFAULT_FILE_BUDGET_BYTES))
        ctx = ReviewContext(
            repo=imported.repo,
            pr_number=imported.pr_number,
            base_sha=imported.base_sha,
            head_sha=imported.head_sha,
            diff_text=imported.diff_text,
            git_history=collect_git_history(repo_dir_path, imported.base_sha, imported.head_sha),
            related_files=collect_related_files(repo_dir_path, imported.diff_text, budget_bytes=budget),
            ci_status=collect_ci_status(provider, imported.head_sha) if provider else None,
            command=command,
            options=options or {},
            user_context=user_context,
            source_dir=str(repo_dir_path),
            pr_key=imported.pr_key,
        )
    persist_review_context(ctx)
    return ctx


def persist_review_context(ctx: ReviewContext) -> Path:
    path = context_path(ctx.pr_key or "unknown")
    path.write_text(ctx.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_review_context(pr_key: str) -> ReviewContext | None:
    path = context_path(pr_key)
    if not path.is_file():
        return None
    return ReviewContext.model_validate_json(path.read_text(encoding="utf-8"))
