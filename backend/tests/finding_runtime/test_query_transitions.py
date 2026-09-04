from __future__ import annotations

import json
from pathlib import Path

from app.services.contracts.models import RuntimeContinueReason, RuntimeStopReason

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "query_parity" / "reason_matrix.json"
REASON_MATRIX = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_restored_inspired_continue_reason_matrix_matches_runtime_enum():
    assert [reason.value for reason in RuntimeContinueReason] == REASON_MATRIX["continue_reasons"]


def test_restored_inspired_terminal_reason_matrix_matches_runtime_enum():
    assert [reason.value for reason in RuntimeStopReason] == REASON_MATRIX["terminal_reasons"]
