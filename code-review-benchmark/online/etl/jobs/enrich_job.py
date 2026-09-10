"""Enrich job: long-running daemon that enriches pending PRs via GitHub API.

Continuously picks up pending PRs, enriches them, and assembles them.
When GitHub rate limit is hit, sleeps until reset and resumes.

Usage:
    uv run python -m jobs.enrich_job --chatbot "coderabbitai[bot]"
    uv run python -m jobs.enrich_job --chatbot "coderabbitai[bot]" --one-shot --max-prs 10
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository
from db.schema import create_tables
from pipeline.enrich import enrich_loop

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich pending PRs via GitHub API")
    parser.add_argument("--chatbot", required=True, help="GitHub username of the chatbot")
    parser.add_argument("--one-shot", action="store_true", help="Process available PRs once and exit")
    parser.add_argument("--max-prs", type=int, help="Maximum number of PRs to enrich")
    parser.add_argument("--database-url", help="Override DATABASE_URL from .env")
    parser.add_argument("--github-token", help="Override GITHUB_TOKEN from .env")
    parser.add_argument("--max-pr-commits", type=int, help="Skip PRs with more commits than this (default: 50)")
    parser.add_argument(
        "--max-pr-changed-lines", type=int, help="Skip PRs with more changed lines than this (default: 2000)"
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url
    if args.github_token:
        cfg.github_token = args.github_token
    if args.max_pr_commits is not None:
        cfg.max_pr_commits = args.max_pr_commits
    if args.max_pr_changed_lines is not None:
        cfg.max_pr_changed_lines = args.max_pr_changed_lines

    if not cfg.github_token:
        logger.error("GITHUB_TOKEN required. Set in .env or pass --github-token")
        return

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        repo = PRRepository(db)

        chatbot = await repo.get_chatbot(args.chatbot)
        if not chatbot:
            logger.error(f"Chatbot '{args.chatbot}' not found. Run discover_job first.")
            return

        chatbot_id = chatbot["id"]
        chatbot_username = chatbot["github_username"]

        # Run enrichment
        enriched = await enrich_loop(
            cfg,
            db,
            chatbot_id,
            max_prs=args.max_prs,
            one_shot=args.one_shot,
        )
        logger.info(f"Enriched {enriched} PRs")

        # Assemble enriched PRs
        if enriched > 0:
            from pipeline.assemble import assemble_enriched_prs

            assembled = await assemble_enriched_prs(db, chatbot_id, chatbot_username)
            logger.info(f"Assembled {assembled} PRs")

    finally:
        await db.close()


def main() -> None:
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
