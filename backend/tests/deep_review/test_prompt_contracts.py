import pytest

from app.domains.deep_review.services.prompt_loader import load_prompt


@pytest.mark.parametrize(
    "name, required",
    [
        ("planner", ["FinalizeReview", "ReviewPlan", "target_files"]),
        ("reviewer", ["FinalizeReview", "ReviewerResult", "untrusted"]),
        ("reviewer_fallback", ["FinalizeReview", "ReviewerResult"]),
        ("cross_analysis", ["FinalizeReview", "CrossAnalysisResult"]),
    ],
)
def test_prompt_contract(name: str, required: list[str]) -> None:
    prompt = load_prompt(name)
    for text in required:
        assert text in prompt


def test_unknown_prompt_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        load_prompt("../system")
