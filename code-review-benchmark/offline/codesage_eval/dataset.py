from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from codesage_eval.contracts import DatasetCase, GoldenFinding


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}-{sha256_bytes(value.encode('utf-8'))[:16]}"


def load_dataset(path: str | Path, fixture_overrides: dict[str, Any] | None = None) -> list[DatasetCase]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("benchmark_data.json must be an object keyed by PR URL")
    overrides = fixture_overrides or {}
    cases: list[DatasetCase] = []
    for pr_url, raw in sorted(payload.items()):
        case_id = stable_id("case", pr_url)
        golden = []
        for index, item in enumerate(raw.get("golden_comments") or []):
            serialized = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            golden.append(
                GoldenFinding(
                    golden_id=stable_id(f"{case_id}-golden-{index + 1}", serialized),
                    comment=str(item.get("comment") or ""),
                    severity=item.get("severity"),
                    category=item.get("category"),
                )
            )
        override = dict(overrides.get(pr_url) or {})
        fixture_path = Path(override["path"]).resolve() if override.get("path") else None
        fixture_hash = hash_fixture(fixture_path) if fixture_path and fixture_path.exists() else None
        mode = str(override.get("source_mode") or "fixture_unverified")
        if mode not in {"full_source", "diff_only", "fixture_unverified"}:
            raise ValueError(f"invalid source_mode for {pr_url}: {mode}")
        golden_hash = sha256_bytes(
            json.dumps([item.model_dump() for item in golden], ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        base_ref = override.get("base_ref")
        head_ref = override.get("head_ref")
        merge_base = override.get("merge_base")
        original_base_tip = override.get("original_base_tip")
        diff_hash = None
        changed_lines = count_changed_lines(fixture_path) if fixture_path and fixture_path.is_file() else None
        if mode == "diff_only":
            if not fixture_path or not fixture_path.is_file() or fixture_path.suffix != ".diff":
                mode = "fixture_unverified"
            else:
                diff_hash = fixture_hash
        elif mode == "full_source":
            verification = verify_git_fixture(fixture_path, base_ref, head_ref, merge_base)
            if verification is None:
                mode = "fixture_unverified"
            else:
                base_ref, head_ref, merge_base, diff_hash, changed_lines = verification
                original_base_tip = original_base_tip or base_ref
        cases.append(
            DatasetCase(
                case_id=case_id,
                pr_url=pr_url,
                repo=str(raw.get("source_repo") or "unknown"),
                pr_title=str(raw.get("pr_title") or ""),
                golden=golden,
                source_mode=mode,
                fixture_path=str(fixture_path) if fixture_path else None,
                fixture_sha256=fixture_hash,
                base_ref=base_ref,
                head_ref=head_ref,
                merge_base=merge_base,
                original_base_tip=original_base_tip,
                diff_sha256=diff_hash,
                changed_lines=changed_lines,
                golden_sha256=golden_hash,
            )
        )
    return cases


def hash_fixture(path: Path) -> str:
    if path.is_file():
        return sha256_bytes(path.read_bytes())
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file() and ".git" not in item.parts):
        digest.update(child.relative_to(path).as_posix().encode("utf-8"))
        digest.update(child.read_bytes())
    return digest.hexdigest()


def count_changed_lines(path: Path) -> int:
    if path.suffix != ".diff":
        return 0
    total = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if (line.startswith("+") and not line.startswith("+++")) or (
            line.startswith("-") and not line.startswith("---")
        ):
            total += 1
    return total


def verify_git_fixture(
    path: Path | None,
    base_ref: str | None,
    head_ref: str | None,
    expected_merge_base: str | None,
) -> tuple[str, str, str, str, int] | None:
    """Resolve immutable Git inputs and hash the exact merge-base-to-head diff."""
    if not path or not path.is_dir() or not base_ref or not head_ref or not (path / ".git").exists():
        return None
    try:
        resolved_base = _git(path, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
        resolved_head = _git(path, "rev-parse", "--verify", f"{head_ref}^{{commit}}")
        merge_base = _git(path, "merge-base", resolved_base, resolved_head)
        if expected_merge_base:
            expected = _git(path, "rev-parse", "--verify", f"{expected_merge_base}^{{commit}}")
            if merge_base != expected:
                return None
        diff = subprocess.check_output(
            ["git", "diff", "--binary", "--no-ext-diff", f"{merge_base}..{resolved_head}"], cwd=path
        )
        changed = _git(path, "diff", "--numstat", "--no-ext-diff", f"{merge_base}..{resolved_head}")
        changed_lines = sum(
            int(value)
            for line in changed.splitlines()
            for value in line.split("\t")[:2]
            if value.isdigit()
        )
        return resolved_base, resolved_head, merge_base, sha256_bytes(diff), changed_lines
    except (OSError, subprocess.CalledProcessError):
        return None


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=path, text=True, encoding="utf-8", stderr=subprocess.DEVNULL
    ).strip()


def select_suite(cases: list[DatasetCase], suite: str) -> list[DatasetCase]:
    ordered = sorted(cases, key=lambda item: (item.repo, item.case_id))
    if suite == "full":
        return ordered
    by_repo: dict[str, list[DatasetCase]] = {}
    for case in ordered:
        by_repo.setdefault(case.repo, []).append(case)
    if suite == "smoke":
        return [by_repo[name][0] for name in sorted(by_repo)[:2]]
    calibration: list[DatasetCase] = []
    for name in sorted(by_repo):
        repo_cases = sorted(
            by_repo[name], key=lambda item: (item.changed_lines if item.changed_lines is not None else 10**12, item.case_id)
        )
        calibration.append(repo_cases[0])
        if len(repo_cases) > 1:
            calibration.append(repo_cases[-1])
    calibration = sorted({item.case_id: item for item in calibration}.values(), key=lambda item: item.case_id)
    if suite == "calibration":
        return calibration
    if suite == "holdout":
        calibration_ids = {item.case_id for item in calibration}
        return [item for item in ordered if item.case_id not in calibration_ids]
    raise ValueError(f"unknown suite: {suite}")
