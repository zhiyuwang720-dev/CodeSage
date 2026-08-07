"""Static analysis of Bash command text (Kode packages/permissions core subset).

Checks: rm/rmdir protected targets (deny), write targets outside the working
directories (ask), `cd` + write compounds (ask), injection patterns (ask).

Heuristic only — this is not a shell parser, and a clever command can always
slip through static analysis. Phase 16 adds an LLM intent gate as backstop.
# ponytail: heuristic, LLM gate lands in phase 16.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .paths import is_write_protected

#: Write verbs analyzed per subcommand (rm/rmdir also get deny protection).
_WRITE_VERBS = frozenset({"rm", "rmdir", "mv", "cp", "mkdir"})

#: rm/rmdir targets that are always refused.
_RM_PROTECTED = frozenset({"/", "/home", "/root", "~", ""})

#: Redirection like `>` / `>>` / `2>` (optionally fd-numbered).
_REDIR_RE = re.compile(r"^[0-9]*>{1,2}")

#: Injection patterns — conservative: any hit asks (even inside quotes).
_INJECTION_PATTERNS = ("$(", "`", "${", "<(", ">(", "IFS=", "<<")


@dataclass(slots=True)
class BashAnalysis:
    """Command-level verdict: 'allow' | 'ask' | 'deny' + reason."""

    decision: str
    reason: str


def split_commands(cmd: str) -> list[str]:
    """Split on &&/||/;/|/newline, ignoring quoted sections."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            if ch == "\\" and i + 1 < n:  # escaped char inside quotes
                buf.append(ch)
                buf.append(cmd[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif cmd.startswith("&&", i) or cmd.startswith("||", i):
            parts.append("".join(buf))
            buf = []
            i += 1
        elif ch in ";|\n":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:  # unterminated quotes — fall back to whitespace split
        return cmd.split()


def _strip_quotes(tok: str) -> str:
    return tok.strip("'\"")


def _is_write_verb(tok: str) -> bool:
    return tok in _WRITE_VERBS


def _in_working_dirs(target: Path, working_dirs: list[Path], cwd: Path) -> bool:
    try:
        p = target.expanduser()
        if not p.is_absolute():
            p = cwd / p
        p = p.resolve()
    except OSError:
        return False
    return any(p.is_relative_to(wd) for wd in working_dirs)


def _redirect_targets(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for i, tok in enumerate(tokens):
        m = _REDIR_RE.match(tok)
        if not m:
            continue
        rest = tok[m.end():]
        if rest.startswith("&"):  # fd duplication like 2>&1, not a file
            continue
        target = rest or (tokens[i + 1] if i + 1 < len(tokens) else "")
        if target and _strip_quotes(target) not in ("/dev/null",):
            out.append(_strip_quotes(target))
    return out


def _operands(tokens: list[str], start: int, stop: int) -> list[str]:
    """Non-flag tokens in tokens[start:stop] ('--' ends flag parsing)."""
    out: list[str] = []
    flags = True
    for tok in tokens[start:stop]:
        if flags and tok == "--":
            flags = False
            continue
        if flags and tok.startswith("-") and tok != "-":
            continue
        out.append(tok)
    return out


def _write_targets(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """(verb, operands) pairs for every write verb in the subcommand."""
    pairs: list[tuple[str, list[str]]] = []
    verb_idx = [i for i, t in enumerate(tokens) if _is_write_verb(t)]
    for i, verb in enumerate(verb_idx):
        stop = verb_idx[i + 1] if i + 1 < len(verb_idx) else len(tokens)
        pairs.append((tokens[verb], _operands(tokens, verb + 1, stop)))
    return pairs


def analyze_bash_command(
    command: str,
    *,
    working_dirs: list[Path],
    cwd: Path,
) -> BashAnalysis:
    """Verdict for one command line. Callers decide what ask/deny means."""
    subs = split_commands(command)

    # Injection patterns (checked on the raw text — conservative).
    if any(p in command for p in _INJECTION_PATTERNS):
        return BashAnalysis("ask", "command contains an injection pattern")

    tokenized = [_tokens(sub) for sub in subs]
    has_cd = any(toks and toks[0] == "cd" for toks in tokenized)
    has_write = False

    for toks in tokenized:
        # `=cmd` expansion (zsh: `=curl evil.com` runs curl) — a token whose
        # very first character is `=` with content after it. FOO=bar has the
        # `=` mid-token and does not hit.
        if any(tok.startswith("=") and len(tok) > 1 for tok in toks):
            return BashAnalysis("ask", "command contains a =command expansion")

        # rm/rmdir deny protection + write-target working-dir checks.
        for verb, operands in _write_targets(toks):
            has_write = True
            if verb in ("rm", "rmdir"):
                if not operands:
                    return BashAnalysis("deny", f"{verb} with no target refused")
                for op in operands:
                    t = _strip_quotes(op)
                    if t in _RM_PROTECTED or t.rstrip("/") in _RM_PROTECTED or t == "~":
                        return BashAnalysis("deny", f"{verb} of protected path {t!r} refused")
                    try:
                        t_path = Path(t).expanduser()
                        if not t_path.is_absolute():
                            t_path = cwd / t_path
                        if t_path.resolve() == cwd.resolve():
                            return BashAnalysis("deny", f"{verb} of the working directory refused")
                    except OSError:
                        return BashAnalysis("deny", f"{verb} of unresolvable path {t!r} refused")
            # mv/cp/mkdir: the last operand is the write destination.
            if verb in ("mv", "cp", "mkdir") and operands:
                if not _in_working_dirs(Path(operands[-1]), working_dirs, cwd):
                    return BashAnalysis("ask", f"{verb} writes outside the working directories")
            # rm targets outside the working directories are asks, not denys.
            if verb in ("rm", "rmdir"):
                for op in operands:
                    if not _in_working_dirs(Path(_strip_quotes(op)), working_dirs, cwd):
                        return BashAnalysis("ask", f"{verb} targets a path outside the working directories")

        # sed -i edits in place: its last operand is a write target.
        if toks and toks[0] == "sed" and any(t.startswith("-i") for t in toks):
            operands = _operands(toks, 1, len(toks))
            if operands and not _in_working_dirs(Path(operands[-1]), working_dirs, cwd):
                return BashAnalysis("ask", "sed -i writes outside the working directories")

        # Redirection targets must stay inside the working directories.
        for target in _redirect_targets(toks):
            has_write = True
            if not _in_working_dirs(Path(target), working_dirs, cwd):
                return BashAnalysis("ask", f"redirects to {target!r} outside the working directories")

    # `cd` + write compound: the final cwd is unknowable statically.
    if has_cd and has_write:
        return BashAnalysis("ask", "cd compound with a write operation")

    return BashAnalysis("allow", "no bash rules matched")


def rm_protected_targets(command: str, cwd: Path) -> list[str]:
    """rm/rmdir 目标中命中写保护组件的路径(阶段 09 §5.3:floor_check 的 Bash 分支用)。

    analyze_bash_command 的 deny 只覆盖 _RM_PROTECTED(/,~ 等字面值);写保护组件
    (.git/.ssh/settings.json 等,paths.py)与文件工具同等地板,这里补查,返回命中列表。
    """
    hits: list[str] = []
    for sub in split_commands(command):
        for verb, operands in _write_targets(_tokens(sub)):
            if verb not in ("rm", "rmdir"):
                continue
            for op in operands:
                t = _strip_quotes(op)
                if not t or t in _RM_PROTECTED:
                    continue
                try:
                    p = Path(t).expanduser()
                    if not p.is_absolute():
                        p = cwd / p
                    protected = is_write_protected(p)
                except OSError:
                    protected = True  # unresolvable — conservative
                if protected:
                    hits.append(t)
    return hits
