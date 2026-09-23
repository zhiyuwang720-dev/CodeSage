"""Opt-in AACR planning smoke; never collected as a normal pytest test."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.base import Base
import app.models.audit_session  # Register the isolated RuntimeBridge tables before create_all.
from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.services.runtime import RuntimeBridgeDeepReviewRuntimeFactory
from app.domains.deep_review.services.service import DeepReviewService
from app.domains.deep_review.storage.jsonl_repository import JsonlDeepReviewStore
from app.execution_plane.models.service import LLMService


def _load_cases(path: Path) -> dict[str, dict]:
    cases: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            cases[item["instance_id"]] = item
    return cases


async def _run_case(case: dict, *, repo_cache: Path, output_root: Path, turns: int, timeout: int) -> dict:
    case_id = str(case["instance_id"])
    repo_slug = str(case["repo"])
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", case_id) or case_id in {".", ".."}:
        raise ValueError("unsafe case id")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_slug):
        raise ValueError("unsafe repository slug")
    repo = (repo_cache / repo_slug.replace("/", "__")).resolve()
    if not repo.is_dir() or not repo.is_relative_to(repo_cache.resolve()):
        raise FileNotFoundError(f"cached repository unavailable: {repo_slug}")

    case_dir = output_root / case_id
    case_dir.mkdir(parents=True, exist_ok=False)
    engine = create_engine(URL.create("sqlite", database=str(case_dir / "sessions.sqlite3")))
    try:
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        config = DeepReviewConfig(max_turns_planner=turns)
        llm_service = LLMService()
        llm_service.get_config_for(config.semantic_role)
        llm_service.get_config_for(config.planner_role)
        service = DeepReviewService(
            config=config,
            store=JsonlDeepReviewStore(case_dir / "events"),
            runtime_factory=RuntimeBridgeDeepReviewRuntimeFactory(
                llm_service=llm_service, session_factory=session_factory,
            ),
        )
        started = time.monotonic()
        report = await asyncio.wait_for(
            service.run(
                ReviewInput(
                    repo_path=str(repo),
                    base_ref=case["base_commit"],
                    head_ref=case["head_commit"],
                ),
                through="planning",
            ),
            timeout=timeout,
        )
        (case_dir / "report.json").write_text(
            report.model_dump_json(indent=2) + "\n", encoding="utf-8",
        )
        active = {
            path for dimension in report.plan.dimensions if not dimension.deferred
            for path in dimension.target_files
        }
        deferred = {
            path for dimension in report.plan.dimensions if dimension.deferred
            for path in dimension.target_files
        }
        comments = case.get("reference_comments") or []
        result = {
            "case_id": case_id,
            "status": "planning_completed",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "run_id": report.run_id,
            "semantic_source": report.semantic.source,
            "semantic_confidence": report.semantic.confidence,
            "review_files": len(report.review_paths),
            "active_dimensions": sum(not item.deferred for item in report.plan.dimensions),
            "deferred_dimensions": sum(item.deferred for item in report.plan.dimensions),
            "coverage_complete": report.plan.coverage_complete,
            "reference_comments": len(comments),
            "reference_paths_active": sum(item["path"] in active for item in comments),
            "reference_paths_deferred": sum(item["path"] in deferred for item in comments),
            "observations": [item.model_dump(mode="json") for item in report.agent_observations],
        }
        (case_dir / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        return result
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--case", action="append", required=True)
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--allow-model-calls", action="store_true")
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error("real API calls require --allow-model-calls")
    if len(args.case) > 3 or args.max_turns < 1 or args.timeout_seconds < 1:
        parser.error("use at most three cases and positive limits")
    cases = _load_cases(args.dataset)
    for case_id in args.case:
        if case_id not in cases:
            parser.error(f"unknown case: {case_id}")
        try:
            result = asyncio.run(_run_case(
                cases[case_id], repo_cache=args.repo_cache,
                output_root=args.output_root, turns=args.max_turns,
                timeout=args.timeout_seconds,
            ))
        except Exception as exc:
            print(json.dumps({"case_id": case_id, "status": "failed", "error_type": type(exc).__name__}))
            return 1
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
