"""spec §6 test_webhook_idempotent: 同 (repo, pr, head_sha) 重复事件只创建一个任务。"""
import hashlib
import hmac
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints import pr_webhook

SECRET = "whsec-test"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def payload(head_sha: str) -> bytes:
    return json.dumps(
        {
            "action": "synchronize",
            "number": 5,
            "pull_request": {"number": 5, "url": "https://api.github.com/repos/o/r/pulls/5", "head": {"sha": head_sha}},
            "repository": {"full_name": "o/r"},
        }
    ).encode()


def test_duplicate_event_creates_one_task(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    calls: list[dict] = []
    monkeypatch.setattr(pr_webhook, "run_review_pipeline", lambda **kw: calls.append(kw))

    app = FastAPI()
    app.include_router(pr_webhook.router, prefix="/pr-webhook")
    client = TestClient(app)

    sha = "b" * 40
    first = client.post(
        "/pr-webhook/github", content=payload(sha), headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(payload(sha))}
    )
    assert first.status_code == 202
    assert first.json()["status"] == "accepted"

    second = client.post(
        "/pr-webhook/github", content=payload(sha), headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(payload(sha))}
    )
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(calls) == 1, "重复事件不创建第二个任务"


def test_new_head_sha_runs_again(monkeypatch):
    """同一 PR 新 head(synchronize 推送) → 视为新事件。"""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    calls: list[dict] = []
    monkeypatch.setattr(pr_webhook, "run_review_pipeline", lambda **kw: calls.append(kw))

    app = FastAPI()
    app.include_router(pr_webhook.router, prefix="/pr-webhook")
    client = TestClient(app)

    for sha in ("c" * 40, "d" * 40):
        body = payload(sha)
        resp = client.post(
            "/pr-webhook/github", content=body, headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(body)}
        )
        assert resp.status_code == 202
    assert len(calls) == 2
