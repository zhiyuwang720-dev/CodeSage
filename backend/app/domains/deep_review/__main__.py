from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.services.service import DeepReviewService
from app.domains.deep_review.storage.jsonl_repository import JsonlDeepReviewStore
from app.domains.deep_review.storage.protocol import DeepReviewStoreError


logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.domains.deep_review",
        description="Run the CodeSageDeep offline review pipeline.",
    )
    parser.add_argument("--repo", required=True, help="Path to a local Git worktree")
    parser.add_argument("--base", required=True, help="Base commit or ref")
    parser.add_argument("--head", required=True, help="Head commit or ref")
    parser.add_argument("--through", choices=["anatomy", "planning", "review"], default=None)
    parser.add_argument("--title", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--include", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--store-dir", default=".codesage/deep-review")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write the current stage report JSON to this path; the filename does not change its contents",
    )
    return parser


def _create_temporary_sqlite_runtime(
    config: DeepReviewConfig,
    database_path: Path,
    *,
    include_reviewer: bool = False,
):
    # TEMPORARY SQLITE SESSION STORE BEGIN
    # Deep Review prototype only: RuntimeBridge still stores Harness sessions via AuditSessionStore.
    # Remove this block when Deep Review has its own backend-neutral Runtime session store.
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    import app.models.audit_session  # noqa: F401 - register RuntimeBridge tables with Base.metadata.
    from app.domains.deep_review.services.runtime import RuntimeBridgeDeepReviewRuntimeFactory
    from app.execution_plane.models.service import LLMService

    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(URL.create("sqlite", database=str(database_path)))
    try:
        Base.metadata.create_all(bind=engine)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        llm_service = LLMService()
        # Validate all roles needed for the requested model-backed stage.
        llm_service.get_config_for(config.semantic_role)
        llm_service.get_config_for(config.planner_role)
        if include_reviewer:
            llm_service.get_config_for(config.reviewer_role)
        runtime_factory = RuntimeBridgeDeepReviewRuntimeFactory(
            llm_service=llm_service,
            session_factory=session_factory,
        )
    except Exception:
        engine.dispose()
        raise
    # TEMPORARY SQLITE SESSION STORE END
    return runtime_factory, engine


async def _run(args: argparse.Namespace) -> int:
    config = DeepReviewConfig(
        max_concurrent_reviewers=args.max_concurrency,
        include_paths=args.include,
        exclude_paths=args.exclude,
    )
    review_input = ReviewInput(
        repo_path=args.repo,
        base_ref=args.base,
        head_ref=args.head,
        title=args.title,
        description=args.description,
    )
    runtime_factory = None
    session_engine = None
    requested_stage = args.through or "anatomy"
    artifact_dir = (
        args.output.parent if args.output is not None else Path(args.store_dir)
    ).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if requested_stage in {"planning", "review"}:
        runtime_factory, session_engine = _create_temporary_sqlite_runtime(
            config,
            artifact_dir / "sessions.sqlite3",
            include_reviewer=requested_stage == "review",
        )
    service = DeepReviewService(
        config=config,
        store=JsonlDeepReviewStore(args.store_dir),
        runtime_factory=runtime_factory,
    )
    try:
        report = await service.run(review_input, through=requested_stage)
        encoded = report.model_dump_json(indent=2)
        if args.output is None:
            sys.stdout.write(encoded + "\n")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded + "\n", encoding="utf-8")

        summary = {
            "status": "completed",
            "run_id": report.run_id,
            "mode": report.mode,
            "completed_stage": report.completed_stage,
            "pipeline_complete": report.pipeline_complete,
            "base_commit": report.base_commit,
            "head_commit": report.head_commit,
            "review_files": len(report.review_paths),
            "context_files": len(report.context_paths),
            "dimension_count": len(report.plan.dimensions) if report.plan is not None else 0,
            "reviewer_dimensions_started": report.reviewer_dimensions_started,
            "reviewer_dimensions_succeeded": report.reviewer_dimensions_succeeded,
            "reviewer_dimensions_failed": report.reviewer_dimensions_failed,
            "reviewer_dimensions_deferred": report.reviewer_dimensions_deferred,
            "reviewer_dimensions_degraded": report.reviewer_dimensions_degraded,
            "candidate_count": report.candidate_count,
            "semantic_source": report.semantic.source if report.semantic is not None else None,
            "observations": [item.model_dump(mode="json") for item in report.agent_observations],
            "report_path": str(args.output.resolve()) if args.output is not None else None,
            "sessions_database": (
                str(artifact_dir / "sessions.sqlite3") if session_engine is not None else None
            ),
            "event_store": str(Path(args.store_dir).resolve()),
        }
        (artifact_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0
    except Exception as exc:
        failure_summary = {
            "status": "failed",
            "requested_stage": requested_stage,
            "base_ref": args.base,
            "head_ref": args.head,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "sessions_database": (
                str(artifact_dir / "sessions.sqlite3") if session_engine is not None else None
            ),
            "event_store": str(Path(args.store_dir).resolve()),
        }
        (artifact_dir / "summary.json").write_text(
            json.dumps(failure_summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        raise
    finally:
        if session_engine is not None:
            session_engine.dispose()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except ValueError as exc:
        logger.error("deep_review.input_or_config_error error=%s", exc)
        return 2
    except (DeepReviewStoreError, OSError, RuntimeError) as exc:
        logger.exception("deep_review.run_failed error=%s", exc)
        return 1
    except asyncio.CancelledError:
        logger.warning("deep_review.cancelled")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
