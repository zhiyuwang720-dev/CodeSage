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
        description="Run the CodeSageDeep offline preparation pipeline.",
    )
    parser.add_argument("--repo", required=True, help="Path to a local Git worktree")
    parser.add_argument("--base", required=True, help="Base commit or ref")
    parser.add_argument("--head", required=True, help="Head commit or ref")
    parser.add_argument("--through", choices=["anatomy", "planning"], default=None)
    parser.add_argument("--title", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--include", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--store-dir", default=".codesage/deep-review")
    parser.add_argument("--output", type=Path, default=None)
    return parser


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
    requested_stage = args.through or "anatomy"
    if requested_stage == "planning":
        from app.domains.deep_review.services.runtime import (
            RuntimeBridgeDeepReviewRuntimeFactory,
        )
        from app.execution_plane.models.service import LLMService

        llm_service = LLMService()
        # Validate both roles before the first paid Semantic request.
        llm_service.get_config_for(config.semantic_role)
        llm_service.get_config_for(config.planner_role)
        runtime_factory = RuntimeBridgeDeepReviewRuntimeFactory(llm_service=llm_service)
    service = DeepReviewService(
        config=config,
        store=JsonlDeepReviewStore(args.store_dir),
        runtime_factory=runtime_factory,
    )
    report = await service.run(review_input, through=requested_stage)
    encoded = report.model_dump_json(indent=2)
    if args.output is None:
        sys.stdout.write(encoded + "\n")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0


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
