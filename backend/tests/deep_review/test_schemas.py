from app.domains.deep_review.schemas import (
    CrossAnalysisResult,
    DeepReviewConfig,
    DeepReviewResult,
    ReviewInput,
    ReviewMetrics,
    ReviewPlan,
)


def test_review_input_round_trip() -> None:
    payload = {"repo_path": "repo", "base_ref": "base", "head_ref": "head"}
    model = ReviewInput.model_validate(payload)
    assert model.model_dump(include={"repo_path", "base_ref", "head_ref"}) == payload


def test_default_config_uses_plan_v3_1_limits() -> None:
    config = DeepReviewConfig()
    assert config.max_planner_dimensions == 8
    assert config.max_final_dimensions == 9
    assert config.max_cross_context_bytes is None
    assert config.max_candidate_count is None


def test_final_result_round_trip() -> None:
    result = DeepReviewResult(
        run_id="run",
        status="partial",
        summary="degraded",
        unresolved_risks=["deferred review"],
        metrics=ReviewMetrics(dimensions=2, model_calls=2, total_tokens=8),
    )
    restored = DeepReviewResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert restored.metrics.usage_complete is False
    assert restored.metrics.cost_available is False


def test_plan_and_cross_schema_round_trip() -> None:
    plan = ReviewPlan(summary="s")
    cross = CrossAnalysisResult(summary="c")
    assert ReviewPlan.model_validate_json(plan.model_dump_json()) == plan
    assert CrossAnalysisResult.model_validate_json(cross.model_dump_json()) == cross
