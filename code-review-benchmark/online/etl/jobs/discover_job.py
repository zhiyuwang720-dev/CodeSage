"""Discover job: run BQ discovery for chatbot(s) and insert new PRs.

Usage:
    uv run python -m jobs.discover_job --chatbot "coderabbitai[bot]" --days-back 7
    uv run python -m jobs.discover_job --all-chatbots --days-back 30
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC
from datetime import datetime
from datetime import timedelta
import logging

from config import DEFAULT_CHATBOT_USERNAMES
from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository
from db.schema import create_tables
from pipeline.discover import discover_prs
from pipeline.discover import discover_prs_batch

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover new PRs from BigQuery")
    parser.add_argument("--chatbot", help="GitHub username of the chatbot to discover PRs for")
    parser.add_argument("--all-chatbots", action="store_true", help="Run discovery for all registered chatbots")
    parser.add_argument("--days-back", type=int, default=7, help="How many days back to search (default: 7)")
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD), overrides --days-back")
    parser.add_argument("--end-date", help="End date (YYYY-MM-DD), defaults to today")
    parser.add_argument("--min-pr-number", type=int, default=0, help="Minimum PR number filter")
    parser.add_argument("--max-prs-per-day", type=int, default=500, help="Random sample cap per day (default: 500)")
    parser.add_argument("--display-name", help="Display name for new chatbot")
    parser.add_argument("--database-url", help="Override DATABASE_URL from .env")
    parser.add_argument("--gcp-project", help="Override GCP_PROJECT from .env")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url
    if args.gcp_project:
        cfg.gcp_project = args.gcp_project

    end_date = args.end_date or datetime.now(UTC).strftime("%Y-%m-%d")
    if args.start_date:
        start_date = args.start_date
    else:
        start_dt = datetime.now(UTC) - timedelta(days=args.days_back)
        start_date = start_dt.strftime("%Y-%m-%d")

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        repo = PRRepository(db)

        if args.all_chatbots:
            chatbots = await repo.get_all_chatbots()
            db_usernames = {bot["github_username"] for bot in chatbots}
            usernames = sorted(db_usernames | set(DEFAULT_CHATBOT_USERNAMES))
            logger.info(f"Batch discovering PRs for {len(usernames)} chatbots: {usernames}")
            await discover_prs_batch(
                cfg,
                db,
                usernames,
                start_date,
                end_date,
                min_pr_number=args.min_pr_number,
                max_prs_per_day=args.max_prs_per_day,
            )
        elif args.chatbot:
            await discover_prs(
                cfg,
                db,
                args.chatbot,
                start_date,
                end_date,
                min_pr_number=args.min_pr_number,
                max_prs_per_day=args.max_prs_per_day,
                display_name=args.display_name,
            )
        else:
            logger.error("Specify --chatbot or --all-chatbots")
    finally:
        await db.close()


def main() -> None:
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
