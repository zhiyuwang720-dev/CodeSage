"""Spec 20P0 配置迁移表与重试预算表校验（P02,P05）。"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent


def _load(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def test_config_mapping_rows_are_actionable() -> None:
    payload = _load("config-mapping.json")
    assert payload["model_boundary_version"] == "litellm_sdk_v1"
    rows = payload["rows"]
    assert rows
    for row in rows:
        assert row["old_field"]
        assert isinstance(row["supported"], bool)
        assert row["fixture_or_regression"], row["old_field"]
        if row["supported"]:
            assert row["sdk_value"], row["old_field"]
            assert row["transport_family"], row["old_field"]
        else:
            assert row["reason"], row["old_field"]
            assert row["sdk_value"] is None


def test_config_mapping_does_not_claim_native_protocol_support() -> None:
    payload = _load("config-mapping.json")
    unsupported = {row["old_field"] for row in payload["rows"] if not row["supported"]}
    assert any("openai_responses" in field for field in unsupported)
    assert any("native" in field for field in unsupported)


def test_retry_budget_has_one_owner_per_row() -> None:
    payload = _load("retry-budget.json")
    rows = payload["rows"]
    assert rows
    owners = payload["owners"]
    for row in rows:
        assert row["new_owner"] in owners, row["scenario"]
        assert row["new_budget"] is not None, row["scenario"]
        assert row["new_budget_meaning"], row["scenario"]
        assert row["verified_by"], row["scenario"]
        # 旧配置必须显式说明叠加点或明确说明不是模型传输重试
        if row["new_owner"] == "control_plane":
            assert "not a model transport retry" in row["new_budget_meaning"]
        else:
            assert row["old_multiplier_points"], row["scenario"]


def test_retry_budget_records_the_spec_deviation() -> None:
    payload = _load("retry-budget.json")
    independent = [row for row in payload["rows"] if row["scenario"].startswith("Independent")]
    assert len(independent) == 1
    row = independent[0]
    assert row["new_owner"] == "sdk_client"
    assert row["new_budget"] == 3
    # 与 Spec P05 表格的偏差必须写明原因，不能静默改口
    assert "cannot bound its own retries" in row["reason"]


def test_retry_budget_keeps_admission_control() -> None:
    payload = _load("retry-budget.json")
    assert "semaphore" in payload["admission_control"]["mechanism"]
