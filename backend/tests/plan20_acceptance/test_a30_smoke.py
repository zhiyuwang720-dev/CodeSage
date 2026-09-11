"""A30 preflight: explicit paid gate and bounded fixed-case smoke."""

from __future__ import annotations

import pytest

from app.diagnostics.__main__ import main
from app.diagnostics.smoke import FIXED_CASE_ID, run_smoke


@pytest.mark.asyncio
async def test_smoke_refuses_without_allow_paid() -> None:
    with pytest.raises(PermissionError):
        await run_smoke(case_id=FIXED_CASE_ID, allow_paid=False, max_cost="0.01", currency="USD")


@pytest.mark.asyncio
async def test_smoke_refuses_unknown_or_zero_budget_case() -> None:
    with pytest.raises(ValueError):
        await run_smoke(case_id="other-case", allow_paid=True, max_cost="0.01", currency="USD")
    with pytest.raises(ValueError):
        await run_smoke(case_id=FIXED_CASE_ID, allow_paid=True, max_cost="0", currency="USD")


def test_smoke_cli_requires_allow_paid(capsys) -> None:
    code = main(["smoke", "--case-id", FIXED_CASE_ID, "--max-cost", "0.01", "--currency", "USD"])
    assert code == 1
    assert "--allow-paid" in capsys.readouterr().err
