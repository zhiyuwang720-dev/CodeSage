"""AP12：恢复指纹与换 worker 恢复（P06,P10）。"""

from __future__ import annotations

import pytest

from app.contracts.review_execution import ReviewRunIdentity, build_config_fingerprint
from app.control_plane.review_policy import build_review_compatibility_config
from app.execution_plane.models.config import MODEL_BOUNDARY_VERSION

SHA = "a" * 64


class _Task:
    llm_config = {"provider": "deepseek", "model": "deepseek-chat", "temperature": 0.1, "max_tokens": 1024}
    max_iterations = 10
    timeout_seconds = 600
    token_budget = 5000


def _identity(fingerprint: str) -> ReviewRunIdentity:
    return ReviewRunIdentity(
        run_id="run-1",
        task_id="task-1",
        source_kind="diff",
        repository_key="repo",
        diff_sha256=SHA,
        config_fingerprint=fingerprint,
    )


def test_ap12_fingerprint_includes_model_boundary_version() -> None:
    config = build_review_compatibility_config(_Task())
    assert config["model_boundary_version"] == MODEL_BOUNDARY_VERSION
    assert config["runtime"]["model_boundary_version"] == MODEL_BOUNDARY_VERSION
    fingerprint = build_config_fingerprint(config)
    assert len(fingerprint) == 64

    mutated = dict(config)
    mutated["model_boundary_version"] = "legacy_adapter_v0"
    assert build_config_fingerprint(mutated) != fingerprint


def test_ap12_old_identity_is_rejected_without_rewriting() -> None:
    """旧指纹（无边界版本）与新指纹不一致时必须拒绝复用，且不改写旧身份。"""

    legacy_config = build_review_compatibility_config(_Task())
    legacy_config.pop("model_boundary_version")
    legacy_config["runtime"].pop("model_boundary_version")
    legacy_fingerprint = build_config_fingerprint(legacy_config)
    new_fingerprint = build_config_fingerprint(build_review_compatibility_config(_Task()))
    assert legacy_fingerprint != new_fingerprint

    persisted = _identity(legacy_fingerprint)
    candidate = _identity(new_fingerprint)
    comparable = candidate.model_copy(update={"run_id": persisted.run_id})
    assert comparable != persisted
    # 旧身份对象本身不因比较被改写
    assert persisted.config_fingerprint == legacy_fingerprint


@pytest.mark.skip(reason="AP12 L2 由 tests/worker_acceptance/test_worker_cancel_resume.py 在隔离 Postgres/Redis 上执行")
@pytest.mark.asyncio
async def test_ap12_resume_on_another_worker_keeps_completed_stages() -> None:  # pragma: no cover - L2
    """L2：新任务完成一个视角后取消并换 worker 恢复；已完成阶段不得新增模型请求。

    该场景由 `tests/worker_acceptance/test_worker_cancel_resume.py::
    test_cancel_then_resume_on_another_worker_keeps_completed_stage` 在隔离
    Postgres/Redis 上执行三次，断言：
    - 恢复后 worker_id 变化、run_id 不变（身份保持）
    - 已完成视角的模型调用计数不增加
    - 五个阶段全部 completed、findings 类别保持

    本用例只负责把 AP12 的 L2 证据指向该实现，避免第二套等价装配。
    """

    pytest.skip("AP12 L2 由 worker_acceptance/test_worker_cancel_resume.py 承担；见该文件与台账 F02 记录")
