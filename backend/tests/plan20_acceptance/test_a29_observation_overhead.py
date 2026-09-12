"""A29: observation on/off overhead evidence without speed thresholds."""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from pathlib import Path

RUNS = 3
CALLS = 20


def test_a29_observation_overhead_three_runs(acceptance_artifact_root) -> None:
    root = acceptance_artifact_root / "evidence" / "a29"
    records = []
    for mode in ("off", "on"):
        for repetition in range(RUNS):
            output = root / mode / str(repetition)
            command = [
                sys.executable,
                "-m",
                "tests.plan20_acceptance.a29_benchmark",
                "--observability",
                mode,
                "--output",
                str(output),
                "--calls",
                str(CALLS),
            ]
            completed = subprocess.run(
                command,
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert completed.returncode == 0, completed.stderr[-2000:]
            record = json.loads(completed.stdout.strip().splitlines()[-1])
            assert record["content"] == "ok"
            assert record["http_requests"] == CALLS
            record["repetition"] = repetition
            records.append(record)

    root.mkdir(parents=True, exist_ok=True)
    summary = {
        "calls_per_run": CALLS,
        "runs_per_mode": RUNS,
        "records": records,
        "wall_median": {
            mode: statistics.median(
                record["wall_seconds"] for record in records if record["observability"] == mode
            )
            for mode in ("off", "on")
        },
        "cpu_median": {
            mode: statistics.median(
                record["cpu_seconds"] for record in records if record["observability"] == mode
            )
            for mode in ("off", "on")
        },
        "note": "No speedup threshold is asserted; raw six-run measurements are the evidence.",
    }
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
