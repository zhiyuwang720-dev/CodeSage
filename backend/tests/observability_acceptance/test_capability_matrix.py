"""Spec 20P0 能力矩阵校验（P03/D01）。

矩阵描述的是**当前** LiteLLM SDK 单出口路径：发送点、传输族、回调粒度、
usage/cost 能力与已验证的 AP 用例。`verified` 必须能对应到真实 SDK 验收。
"""

from __future__ import annotations

import json
from pathlib import Path

MATRIX_PATH = Path(__file__).with_name("capability-matrix.json")

VERIFIED_VALUES = {"verified", "sdk_candidate_plus_frozen_price_table", "redacted_when_capture_disabled"}
ALLOWED_CAPABILITY_VALUES = VERIFIED_VALUES | {
    "unverified",
    "unavailable",
    "supported_not_in_required_matrix",
    "unknown_without_frozen_price_entry",
    "n/a",
}


def load_matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def test_capability_matrix_declares_the_single_sdk_send_point() -> None:
    payload = load_matrix()
    assert payload["schema_version"] == 2
    sdk_call = payload["sdk_call"]
    assert sdk_call["function"] == "litellm.acompletion"
    assert sdk_call["source_file"].endswith("execution_plane/models/client.py")
    assert sdk_call["symbol"].endswith("_send")
    assert sdk_call["num_retries"] == 0


def test_capability_matrix_records_callback_granularity() -> None:
    surface = load_matrix()["callback_surface"]
    assert surface["otel_span_name"] == "litellm_request"
    assert "request level" in surface["otel_granularity"]
    # 不能把文档中的 request/deployment hook 当成底层 HTTP attempt 证据
    assert "not observable" in surface["underlying_http_retry_granularity"] or "disabled" in surface[
        "underlying_http_retry_granularity"
    ]


def test_capability_matrix_routes_are_complete() -> None:
    routes = load_matrix()["routes"]
    assert routes
    for route in routes:
        assert route["provider"]
        assert route["usage_sources"]
        assert route["acceptance_cases"]
        assert route["verification_command"]
        for field in ("request_capability", "stream_capability", "usage_capability", "callback_capability"):
            assert route[field] in ALLOWED_CAPABILITY_VALUES, (route["provider"], field, route[field])


def test_capability_matrix_required_paths_are_verified() -> None:
    routes = load_matrix()["routes"]
    required = {
        ("deepseek", "openai_chat"),
        ("claude", "anthropic_messages"),
    }
    seen = {(route["provider"], route["protocol"]) for route in routes}
    assert required <= seen
    for route in routes:
        if (route["provider"], route["protocol"]) in required:
            assert route["request_capability"] == "verified"
            assert route["stream_capability"] == "verified"
            assert route["usage_capability"] == "verified"


def test_capability_matrix_declares_known_gaps() -> None:
    payload = load_matrix()
    gaps = payload["known_gaps"]
    assert gaps
    for gap in gaps:
        assert gap["id"] and gap["summary"] and gap["impact"] and gap["evidence"]
    # 不能为了保留旧下拉选项而声称 native 协议受支持
    unavailable = [route for route in payload["routes"] if route["transport_family"] == "unavailable"]
    assert unavailable
    for route in unavailable:
        assert route["request_capability"] == "unavailable"
