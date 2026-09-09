from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol

from codesage_eval.contracts import CandidateFinding, GoldenFinding, JudgmentRecord
from codesage_eval.storage import read_jsonl, write_jsonl_atomic


SERIALIZER_VERSION = "finding_serializer_v1"
JUDGE_PROMPT_VERSION = "codesage_pair_judge_v1"


def serialize_candidate(item: CandidateFinding) -> str:
    location = f"{item.file_path or 'unknown'}:{item.line_start or '?'}-{item.line_end or '?'}"
    return "\n".join(
        [
            f"Title: {item.title}",
            f"Description: {item.description}",
            f"Location: {location}",
            f"Suggestion: {item.suggestion or ''}",
        ]
    )


def cache_key(golden: GoldenFinding, candidate: CandidateFinding, judge_fingerprint: str) -> str:
    payload = "\0".join(
        [judge_fingerprint, JUDGE_PROMPT_VERSION, SERIALIZER_VERSION, golden.comment, serialize_candidate(candidate)]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PairJudge(Protocol):
    fingerprint: str

    def judge(self, golden: GoldenFinding, candidate: CandidateFinding) -> tuple[bool, float, str]: ...


class OpenAICompatiblePairJudge:
    def __init__(self, *, base_url: str, api_key: str, model: str):
        if not base_url or not api_key or not model:
            raise ValueError("judge base_url, api_key and model must be explicit")
        from openai import OpenAI

        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self._model = model
        self.fingerprint = hashlib.sha256(
            f"openai-compatible\0{base_url}\0{model}\0temperature=0\0{JUDGE_PROMPT_VERSION}".encode()
        ).hexdigest()

    def judge(self, golden: GoldenFinding, candidate: CandidateFinding) -> tuple[bool, float, str]:
        prompt = (
            "Decide whether the candidate identifies the same underlying review issue as the golden. "
            "Return JSON only: {\"match\":bool,\"confidence\":0..1,\"reason\":string}.\n\n"
            f"GOLDEN:\n{golden.comment}\n\nCANDIDATE:\n{serialize_candidate(candidate)}"
        )
        last_error: Exception | None = None
        for _ in range(3):
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
                payload = json.loads(response.choices[0].message.content or "{}")
                return bool(payload["match"]), float(payload["confidence"]), str(payload.get("reason") or "")
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"judge failed after three attempts: {last_error}") from last_error


def judge_case(
    *,
    eval_run_id: str,
    case_id: str,
    golden: list[GoldenFinding],
    candidates: list[CandidateFinding],
    judge: PairJudge,
    cache_path: str | Path,
) -> list[JudgmentRecord]:
    cache_file = Path(cache_path)
    cached = {item["cache_key"]: item for item in read_jsonl(cache_file)} if cache_file.exists() else {}
    results = []
    for golden_item in golden:
        for candidate in candidates:
            key = cache_key(golden_item, candidate, judge.fingerprint)
            if key in cached:
                payload = dict(cached[key])
                payload.update({"eval_run_id": eval_run_id, "case_id": case_id, "status": "reused"})
                results.append(JudgmentRecord.model_validate(payload))
                continue
            try:
                matched, confidence, reason = judge.judge(golden_item, candidate)
                record = JudgmentRecord(
                    eval_run_id=eval_run_id,
                    case_id=case_id,
                    golden_id=golden_item.golden_id,
                    candidate_id=candidate.candidate_id,
                    matched=matched,
                    confidence=max(0.0, min(1.0, confidence)),
                    reason=reason,
                    judge_fingerprint=judge.fingerprint,
                    cache_key=key,
                )
            except Exception as exc:
                record = JudgmentRecord(
                    eval_run_id=eval_run_id,
                    case_id=case_id,
                    golden_id=golden_item.golden_id,
                    candidate_id=candidate.candidate_id,
                    matched=None,
                    confidence=None,
                    reason=str(exc),
                    status="unknown",
                    judge_fingerprint=judge.fingerprint,
                    cache_key=key,
                )
            results.append(record)
            if record.status == "computed":
                cached[key] = record.model_dump(mode="json")
    write_jsonl_atomic(cache_file, (cached[key] for key in sorted(cached)))
    return results
