"""spec §6 test_webhook_hmac: 错误签名 403; 正确签名 202 且任务入队。"""
import hashlib
import hmac
import json

import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints import pr_webhook

SECRET = "whsec-test"


def make_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    # 后台审查不真跑(不触网): 打桩 run_review_pipeline
    calls: list[dict] = []

    def fake_pipeline(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(pr_webhook, "run_review_pipeline", fake_pipeline)
    app = FastAPI()
    app.include_router(pr_webhook.router, prefix="/pr-webhook")
    return TestClient(app), calls


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def payload(head_sha: str = "a" * 40, action: str = "opened") -> bytes:
    return json.dumps(
        {
            "action": action,
            "number": 1,
            "pull_request": {"number": 1, "url": "https://api.github.com/repos/o/r/pulls/1", "head": {"sha": head_sha}},
            "repository": {"full_name": "o/r"},
        }
    ).encode()


def test_bad_signature_403(monkeypatch):
    client, _ = make_client(monkeypatch)
    resp = client.post(
        "/pr-webhook/github",
        content=payload(),
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert resp.status_code == 403


def test_missing_signature_403(monkeypatch):
    client, _ = make_client(monkeypatch)
    resp = client.post("/pr-webhook/github", content=payload(), headers={"X-GitHub-Event": "pull_request"})
    assert resp.status_code == 403


def test_valid_signature_202_and_task_enqueued(monkeypatch):
    client, calls = make_client(monkeypatch)
    body = payload()
    resp = client.post(
        "/pr-webhook/github",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(body)},
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    assert len(calls) == 1, "任务已入队执行"


def test_unsupported_event_ignored(monkeypatch):
    client, calls = make_client(monkeypatch)
    body = payload()
    resp = client.post(
        "/pr-webhook/github",
        content=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert calls == []


def test_unhandled_action_ignored(monkeypatch):
    client, calls = make_client(monkeypatch)
    body = payload(action="closed")
    resp = client.post(
        "/pr-webhook/github",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert calls == []
