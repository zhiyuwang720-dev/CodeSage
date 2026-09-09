from __future__ import annotations

from codesage_eval.contracts import CandidateFinding, GoldenFinding


def run_calibration(pairs: list[dict], judge) -> dict:
    if len(pairs) < 20:
        raise ValueError("calibration requires at least 20 fixed pairs")
    rows = []
    for item in pairs:
        golden = GoldenFinding(golden_id=item["pair_id"] + "-golden", comment=item["golden"])
        candidate = CandidateFinding(
            candidate_id=item["pair_id"] + "-candidate",
            title=item["candidate_title"],
            description=item.get("candidate_description", ""),
            file_path=item.get("file_path"),
            line_start=item.get("line_start"),
        )
        attempts = []
        for _ in range(2):
            try:
                matched, confidence, reason = judge.judge(golden, candidate)
                attempts.append({"matched": matched, "confidence": confidence, "reason": reason})
            except Exception as exc:
                attempts.append({"matched": None, "confidence": None, "reason": str(exc)})
        rows.append({**item, "attempts": attempts})
    decided = [row for row in rows if all(attempt["matched"] is not None for attempt in row["attempts"])]
    consistent = sum(row["attempts"][0]["matched"] == row["attempts"][1]["matched"] for row in decided)
    correct = sum(
        row["attempts"][0]["matched"] == row["human_match"]
        and row["attempts"][1]["matched"] == row["human_match"]
        for row in decided
    )
    return {
        "schema_version": "1",
        "judge_fingerprint": judge.fingerprint,
        "pair_count": len(rows),
        "decided_pair_count": len(decided),
        "unknown_pair_count": len(rows) - len(decided),
        "repeat_agreement": consistent / len(decided) if decided else None,
        "two_run_human_accuracy": correct / len(decided) if decided else None,
        "calibration_status": "calibrated" if len(decided) == len(rows) else "judge_uncalibrated",
        "pairs": rows,
    }
