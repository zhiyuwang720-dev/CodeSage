from __future__ import annotations

import httpx

from codesage_eval.bootstrap import ensure_eval_projects, login
from codesage_eval.contracts import DatasetCase


def _case(repo: str = "acme/widget") -> DatasetCase:
    return DatasetCase(case_id="case", pr_url="url", repo=repo, pr_title="", golden=[], golden_sha256="hash")


def test_login_and_project_bootstrap_are_idempotent(tmp_path):
    created = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"access_token": "token"})
        if request.method == "GET":
            projects = ([{"id": "project-1", "name": "eval:acme/widget", "is_active": True}] if created else [])
            return httpx.Response(200, json=projects)
        created.append(request.read())
        return httpx.Response(200, json={"id": "project-1"})

    client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    assert login(api_url="https://api.test", email="demo@example.com", password="secret", client=client) == "token"
    first = ensure_eval_projects(api_url="https://api.test", token="token", cases=[_case()],
                                 projects_root=tmp_path, client=client)
    second = ensure_eval_projects(api_url="https://api.test", token="token", cases=[_case()],
                                  projects_root=tmp_path, client=client)
    assert first == second == {"acme/widget": "project-1"}
    assert len(created) == 1
