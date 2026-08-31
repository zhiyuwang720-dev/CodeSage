"""GitHub webhook endpoint(阶段 01 §4.4)。

并发模式参考 pr-agent servers/github_app.py:38-54 的 FastAPI BackgroundTasks
结构(GitHub MIT); HMAC-SHA256 校验 + (repo, pr, head_sha) 幂等 + 单 PR 并发上限。
不做任何审查逻辑: 校验 → 提取 → 交统一入口(后台执行)。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from app.services.pr_review.command_router import run_review_pipeline
from app.services.pr_review.webhook_guard import webhook_guard

router = APIRouter()

HANDLED_ACTIONS = {"opened", "synchronize", "reopened"}


def _webhook_secret() -> str:
    # 请求时读取(而非模块导入时): 测试可动态设置; 不动 AutoCVE config.py
    return os.getenv("GITHUB_WEBHOOK_SECRET", "")


def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """GitHub HMAC-SHA256(X-Hub-Signature-256)。密钥未配置时拒绝(防误开放)。"""
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _run_background_review(pr_url: str, repo: str, pr_number: int | None, head_sha: str) -> None:
    try:
        run_review_pipeline(pr_url=pr_url, options={"pr_number": pr_number})
    except Exception:
        pass  # 后台失败不影响 webhook 响应; 结果查询接口可见状态
    finally:
        webhook_guard.release(repo, pr_number)


@router.post("/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    raw = await request.body()
    if not verify_signature(raw, request.headers.get("X-Hub-Signature-256"), _webhook_secret()):
        raise HTTPException(status_code=403, detail="无效的 webhook 签名")

    event = request.headers.get("X-GitHub-Event", "")
    if event != "pull_request":
        return {"status": "ignored", "event": event}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="payload 不是合法 JSON")
    action = payload.get("action")
    if action not in HANDLED_ACTIONS:
        return {"status": "ignored", "event": event, "action": action}

    pr = payload.get("pull_request") or {}
    repo_full = (payload.get("repository") or {}).get("full_name", "")
    pr_number = pr.get("number")
    head_sha = (pr.get("head") or {}).get("sha", "")
    pr_url = pr.get("url") or (f"https://github.com/{repo_full}/pull/{pr_number}" if repo_full else "")
    if not repo_full or pr_number is None:
        raise HTTPException(status_code=400, detail="payload 缺少 repository/pull_request 字段")

    decision = webhook_guard.check_and_register(repo_full, pr_number, head_sha)
    if decision == "duplicate":
        return {"status": "duplicate", "pr_key": f"{repo_full}#{pr_number}"}
    if decision == "busy":
        # §7: 单 PR 并发上限, 第二个事件排队等待(202 + deferred 标记)
        return JSONResponse(
            status_code=202,
            content={"status": "deferred", "reason": "同 PR 并发任务已达上限", "pr_key": f"{repo_full}#{pr_number}"},
        )

    background_tasks.add_task(_run_background_review, pr_url, repo_full, pr_number, head_sha)
    return JSONResponse(
        status_code=202,
        content={"status": "accepted", "pr_key": f"{repo_full}#{pr_number}", "head_sha": head_sha},
    )
