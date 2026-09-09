from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from codesage_eval.dataset import sha256_bytes, stable_id

PR_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?$")


def _write_if_changed(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def fetch_current_pr_fixtures(
    *,
    dataset_path: str | Path,
    output_map: str | Path,
    fixtures_root: str | Path,
    case_ids: set[str] | None = None,
    pr_urls: set[str] | None = None,
    github_token: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    dataset = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    output = Path(output_map).resolve()
    root = Path(fixtures_root).resolve()
    existing = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = github_token or os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    owns_client = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        for pr_url in sorted(dataset):
            case_id = stable_id("case", pr_url)
            if case_ids and case_id not in case_ids:
                continue
            if pr_urls and pr_url not in pr_urls:
                continue
            match = PR_RE.match(pr_url)
            if not match:
                raise ValueError(f"unsupported PR URL: {pr_url}")
            owner, repo, number = match.groups()
            api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
            metadata_response = client.get(api_url, headers=headers)
            metadata_response.raise_for_status()
            metadata = metadata_response.json()
            diff_headers = dict(headers)
            diff_headers["Accept"] = "application/vnd.github.v3.diff"
            diff_response = client.get(api_url, headers=diff_headers)
            diff_response.raise_for_status()
            diff = diff_response.content
            if not diff.strip():
                raise RuntimeError(f"GitHub returned an empty diff for {pr_url}")
            diff_path = root / "current" / case_id / "review.diff"
            _write_if_changed(diff_path, diff)
            previous = dict(existing.get(pr_url) or {})
            fetched_at = (previous.get("provenance") or {}).get("fetched_at")
            if previous.get("provenance", {}).get("diff_sha256") != sha256_bytes(diff):
                fetched_at = datetime.now(timezone.utc).isoformat()
            existing[pr_url] = {
                "path": diff_path.relative_to(output.parent).as_posix(),
                "source_mode": "diff_only",
                "base_ref": metadata.get("base", {}).get("sha"),
                "head_ref": metadata.get("head", {}).get("sha"),
                "baseline_eligible": False,
                "provenance": {
                    "kind": "current_pr",
                    "source_url": pr_url,
                    "metadata_url": api_url,
                    "fetched_at": fetched_at,
                    "base_sha": metadata.get("base", {}).get("sha"),
                    "head_sha": metadata.get("head", {}).get("sha"),
                    "diff_sha256": sha256_bytes(diff),
                },
            }
    finally:
        if owns_client:
            client.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(existing, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    _write_if_changed(output, encoded)
    return existing
