from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from pydantic import ValidationError

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.schemas.output import DeepReviewResult, PreparationReport
from app.domains.deep_review.services.service import DeepReviewService
from app.domains.deep_review.services.orchestrator import _safe_error
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
    parser.add_argument("--through", choices=["anatomy", "planning", "review", "cross", "final"], default="final")
    parser.add_argument("--allow-model-calls", action="store_true", help="explicitly allow paid model stages")
    parser.add_argument("--config", type=Path, help="UTF-8 DeepReviewConfig JSON; unknown fields are rejected")
    parser.add_argument("--title", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--include", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--max-concurrency", type=int, default=None)
    parser.add_argument("--store-dir", default=".codesage/deep-review")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="atomically replace this file with the selected report; final writes only DeepReviewResult",
    )
    return parser


def _create_temporary_sqlite_runtime(
    config: DeepReviewConfig,
    database_path: Path,
    *,
    include_reviewer: bool = False,
    include_cross: bool = False,
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
        if include_cross:
            llm_service.get_config_for(config.cross_analysis_role)
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
    requested_stage = args.through
    if requested_stage != "anatomy" and not args.allow_model_calls:
        raise ValueError(f"{requested_stage} requires --allow-model-calls")
    if args.config:
        try:
            config_data = json.loads(args.config.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"cannot read --config: {type(exc).__name__}") from exc
    else:
        config_data = {}
    if not isinstance(config_data, dict):
        raise ValueError("--config must contain a JSON object")
    if args.max_concurrency is not None:
        config_data["max_concurrent_reviewers"] = args.max_concurrency
    if args.include:
        config_data["include_paths"] = args.include
    if args.exclude:
        config_data["exclude_paths"] = args.exclude
    config = DeepReviewConfig.model_validate(config_data)
    repo_path = Path(args.repo).resolve()
    store_dir = Path(args.store_dir).resolve()
    output_path = args.output.resolve() if args.output is not None else None
    review_input = ReviewInput(
        repo_path=str(repo_path),
        base_ref=args.base,
        head_ref=args.head,
        title=args.title,
        description=args.description,
    )
    runtime_factory = None
    session_engine = None
    artifact_dir = (
        output_path.parent if output_path is not None else store_dir
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if requested_stage in {"planning", "review", "cross", "final"}:
        runtime_factory, session_engine = _create_temporary_sqlite_runtime(
            config,
            artifact_dir / "sessions.sqlite3",
            include_reviewer=requested_stage in {"review", "cross", "final"},
            include_cross=requested_stage in {"cross", "final"},
        )
    service = DeepReviewService(
        config=config,
        store=JsonlDeepReviewStore(store_dir),
        runtime_factory=runtime_factory,
    )
    try:
        report = await service.run(review_input, through=requested_stage)
        run_dir = store_dir / report.run_id
        if isinstance(report, DeepReviewResult):
            try:
                persisted_result = DeepReviewResult.model_validate_json(
                    (run_dir / "result.json").read_text(encoding="utf-8")
                )
                process_report = json.loads((run_dir / "process_report.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, ValidationError) as exc:
                raise DeepReviewStoreError("cannot read valid final/process reports") from exc
            if not isinstance(process_report, dict):
                raise DeepReviewStoreError("process report must be a JSON object")
            if (
                persisted_result.run_id != report.run_id
                or persisted_result.content_hash != report.content_hash
                or process_report.get("run_id") != report.run_id
                or process_report.get("final_status") != report.status
                or process_report.get("final_content_hash") != report.content_hash
            ):
                raise DeepReviewStoreError("final and process reports disagree on run/status/hash")
        encoded = report.model_dump_json(indent=2)
        if output_path is None:
            sys.stdout.write(encoded + "\n")
        else:
            _write_json_atomic(output_path, encoded + "\n")


        summary = {
            "status": "completed",
            "run_id": report.run_id,
            "report_path": str(output_path) if output_path is not None else None,
            "result_path": str(run_dir / "result.json") if requested_stage == "final" else None,
            "process_report_path": str(run_dir / "process_report.json") if requested_stage == "final" else None,
            "sessions_database": (
                str(artifact_dir / "sessions.sqlite3") if session_engine is not None else None
            ),
            "event_store": str(run_dir / "events.jsonl"),
        }
        if isinstance(report, PreparationReport):
            summary.update({
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
                "cross_status": report.cross_status,
                "semantic_source": report.semantic.source if report.semantic is not None else None,
                "observations": [item.model_dump(mode="json") for item in report.agent_observations],
            })
        else:
            summary.update({
                "mode": "final", "completed_stage": "final", "pipeline_complete": True,
                "status": report.status, "finding_count": len(report.findings),
                "candidate_count": report.candidate_count,
                "cross_status": report.cross_status,
                "content_hash": report.content_hash,
                "metrics": report.metrics.model_dump(mode="json"),
            })
        _write_json_atomic(artifact_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        return 0
    except Exception as exc:
        failure_summary = {
            "status": "failed",
            "requested_stage": requested_stage,
            "base_ref": args.base,
            "head_ref": args.head,
            "error_type": type(exc).__name__,
            "error": _safe_error(exc),
            "sessions_database": (
                str(artifact_dir / "sessions.sqlite3") if session_engine is not None else None
            ),
            "event_store": str(store_dir),
        }
        _write_json_atomic(artifact_dir / "summary.json", json.dumps(failure_summary, ensure_ascii=False, indent=2) + "\n")
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
    except (ValueError, ValidationError, json.JSONDecodeError) as exc:
        logger.error("deep_review.input_or_config_error error=%s", _safe_error(exc))
        return 2
    except (DeepReviewStoreError, OSError, RuntimeError) as exc:
        logger.error("deep_review.run_failed error_type=%s error=%s", type(exc).__name__, _safe_error(exc))
        return 1
    except asyncio.CancelledError:
        logger.warning("deep_review.cancelled")
        return 130
    except KeyboardInterrupt:
        logger.warning("deep_review.cancelled")
        return 130


def _write_json_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
