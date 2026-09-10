"""Analyze job: run LLM analysis on assembled PRs.

Usage:
    uv run python -m jobs.analyze_job --chatbot "coderabbitai[bot]" --limit 50
    uv run python -m jobs.analyze_job --all --limit 200
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository
from db.schema import create_tables
from pipeline.analyze import analyze_prs

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run LLM analysis on assembled PRs")
    parser.add_argument("--chatbot", help="GitHub username of the chatbot to analyze")
    parser.add_argument("--all", action="store_true", help="Analyze all chatbots")
    parser.add_argument("--limit", type=int, default=100, help="Max PRs to analyze (default: 100)")
    parser.add_argument("--database-url", help="Override DATABASE_URL from .env")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url

    if not cfg.martian_api_key:
        logger.error("MARTIAN_API_KEY required. Set in .env")
        return

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        repo = PRRepository(db)

        if args.all:
            chatbots = await repo.get_all_chatbots()
            if not chatbots:
                logger.error("No chatbots registered.")
                return
            total = 0
            for bot in chatbots:
                n = await analyze_prs(cfg, db, bot["id"], bot["github_username"], limit=args.limit)
                total += n
            logger.info(f"Analyzed {total} PRs total across {len(chatbots)} chatbots")

        elif args.chatbot:
            bot = await repo.get_chatbot(args.chatbot)
            if not bot:
                logger.error(f"Chatbot '{args.chatbot}' not found.")
                return
            n = await analyze_prs(cfg, db, bot["id"], bot["github_username"], limit=args.limit)
            logger.info(f"Analyzed {n} PRs for {args.chatbot}")

        else:
            logger.error("Specify --chatbot or --all")

    finally:
        await db.close()


def main() -> None:
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
