"""PR 审查手动触发/结果查询 API(阶段 01 §3.1)。

POST: 手动提交审查(plain-diff 同步完成 / pr-url 后台执行);
GET:  按 review_id 查询结果(.auditai/reviews/<id>.json)。
"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app.services.pr_review.command_router import run_review_pipeline, run_review_pipeline_async
from app.services.pr_review.paths import review_path

router = APIRouter()


class PrReviewCreate(BaseModel):
    pr_url: str | None = None
    diff_text: str | None = None
    user_context: str | None = None
    command: str = "review"
    engine: Literal["rules", "runtime"] = "rules"
    repo: str | None = None
    pr_number: int | None = None
    file_budget_bytes: int | None = Field(default=None, ge=1)


class PrReviewOut(BaseModel):
    review_id: str
    pr_key: str
    status: Literal["completed", "running"]
    comments: list[dict] = []


def _options(payload: PrReviewCreate) -> dict:
    options: dict = {
        "repo": payload.repo,
        "pr_number": payload.pr_number,
        "engine": payload.engine,
    }
    if payload.file_budget_bytes:
        options["file_budget_bytes"] = payload.file_budget_bytes
    return options


@router.post("", response_model=PrReviewOut)
async def create_pr_review(payload: PrReviewCreate, background_tasks: BackgroundTasks):
    if not payload.pr_url and payload.diff_text is None:
        raise HTTPException(status_code=422, detail="需要 pr_url 或 diff_text 之一")
    if payload.pr_url or payload.engine == "runtime":
        # diff+上下文模式 / LLM 三视角编排: 耗时, 后台执行(结果落盘后经 GET 查询)
        async def _job():
            await run_review_pipeline_async(
                pr_url=payload.pr_url,
                diff_text=payload.diff_text,
                user_context=payload.user_context,
                command=payload.command,
                options=_options(payload),
            )

        background_tasks.add_task(_job)
        # 阶段 02: 受理即返回; 确定性 review_id 随任务表(阶段 03 评测)引入
        return PrReviewOut(review_id="queued", pr_key="", status="running")

    result = run_review_pipeline(
        diff_text=payload.diff_text,
        user_context=payload.user_context,
        command=payload.command,
        options=_options(payload),
    )
    return PrReviewOut(
        review_id=result.review_id,
        pr_key=result.pr_key,
        status="completed",
        comments=[c.model_dump() for c in result.comments],
    )


@router.get("/{review_id}")
async def get_pr_review(review_id: str):
    path = review_path(review_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"审查结果不存在: {review_id}")
    return json.loads(path.read_text(encoding="utf-8"))
