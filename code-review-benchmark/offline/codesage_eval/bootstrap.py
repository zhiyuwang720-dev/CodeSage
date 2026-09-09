from __future__ import annotations

import re
from pathlib import Path

import httpx

from codesage_eval.contracts import DatasetCase


def login(*, api_url: str, email: str, password: str, client: httpx.Client | None = None) -> str:
    owns_client = client is None
    client = client or httpx.Client(base_url=api_url.rstrip("/"), timeout=30)
    try:
        response = client.post("/api/v1/auth/login", data={"username": email, "password": password})
        response.raise_for_status()
        return str(response.json()["access_token"])
    finally:
        if owns_client:
            client.close()


def ensure_eval_projects(
    *,
    api_url: str,
    token: str,
    cases: list[DatasetCase],
    projects_root: str | Path = "/workspace/projects",
    client: httpx.Client | None = None,
) -> dict[str, str]:
    owns_client = client is None
    client = client or httpx.Client(
        base_url=api_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=30
    )
    try:
        response = client.get("/api/v1/projects/")
        response.raise_for_status()
        existing = {str(item["name"]): str(item["id"]) for item in response.json() if item.get("is_active")}
        result: dict[str, str] = {}
        for repo in sorted({item.repo for item in cases}):
            name = f"eval:{repo}"
            if name not in existing:
                slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", repo).strip("-") or "repository"
                path = Path(projects_root) / slug
                path.mkdir(parents=True, exist_ok=True)
                (path / ".codesage-eval-placeholder").touch(exist_ok=True)
                response = client.post(
                    "/api/v1/projects/",
                    json={"name": name, "source_type": "local_directory", "local_path": str(path),
                          "workspace_mode": "in_place", "description": "Isolated CodeSage evaluation project"},
                )
                response.raise_for_status()
                existing[name] = str(response.json()["id"])
            result[repo] = existing[name]
        return result
    finally:
        if owns_client:
            client.close()
