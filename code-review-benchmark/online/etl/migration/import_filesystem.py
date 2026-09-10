"""One-time migration: import existing filesystem data (output/) into the database.

Walks output/{user}/ directories. For each chatbot:
1. Creates chatbot row
2. Reads 01_find_prs.json for PR list
3. For each PR, reads per-PR JSON files (02-06, assembled.json)
4. Inserts into DB with appropriate status based on which files exist
5. Uses ON CONFLICT DO NOTHING for idempotency — safe to re-run

Usage:
    uv run python -m migration.import_filesystem --output-dir output
    uv run python -m migration.import_filesystem --output-dir output --chatbot "coderabbitai[bot]"
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import logging
import os

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository
from db.schema import create_tables

logger = logging.getLogger(__name__)


@dataclass
class TargetPR:
    """PR reference for filesystem import (owner/repo#number)."""

    repo_name: str  # "owner/repo"
    pr_number: int
    pr_url: str

    def owner(self) -> str:
        return self.repo_name.split("/")[0]

    def repo(self) -> str:
        return self.repo_name.split("/")[1]

    def pr_dir(self, user_dir: str) -> str:
        """Return PR directory path: {user_dir}/{owner}/{repo}/{pr_number}"""
        return os.path.join(user_dir, self.owner(), self.repo(), str(self.pr_number))

    def to_dict(self) -> dict:
        return {"repo_name": self.repo_name, "pr_number": self.pr_number, "pr_url": self.pr_url}

    @classmethod
    def from_dict(cls, d: dict) -> TargetPR:
        return cls(repo_name=d["repo_name"], pr_number=d["pr_number"], pr_url=d["pr_url"])


def _load_json(path: str) -> list | dict | None:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _infer_status(pr_dir: str) -> tuple[str, str | None]:
    """Infer status and enrichment_step from which files exist in a PR directory.

    Returns (status, enrichment_step).
    """
    has_assembled = os.path.exists(os.path.join(pr_dir, "assembled.json"))
    has_details = os.path.exists(os.path.join(pr_dir, "06_commit_details_response.json"))
    has_threads = os.path.exists(os.path.join(pr_dir, "05_review_threads_response.json"))
    has_reviews = os.path.exists(os.path.join(pr_dir, "04_reviews_response.json"))
    has_commits = os.path.exists(os.path.join(pr_dir, "03_commits_response.json"))
    has_events = os.path.exists(os.path.join(pr_dir, "02_fetch_events.json"))

    if has_assembled:
        return "assembled", "done"
    if has_details:
        return "enriched", "done"
    if has_threads:
        return "enriching", "threads"
    if has_reviews:
        return "enriching", "reviews"
    if has_commits:
        return "enriching", "commits"
    if has_events:
        return "pending", "bq_events"
    return "pending", None


async def import_user(
    db: DBAdapter,
    output_dir: str,
    username: str,
) -> int:
    """Import a single user's filesystem data into the database. Returns count imported."""
    repo = PRRepository(db)
    user_dir = os.path.join(output_dir, username)

    if not os.path.isdir(user_dir):
        logger.warning(f"Directory not found: {user_dir}")
        return 0

    chatbot_id = await repo.upsert_chatbot(username)

    # Load PR list
    prs_path = os.path.join(user_dir, "01_find_prs.json")
    prs_data = _load_json(prs_path)
    if prs_data is None:
        logger.warning(f"No 01_find_prs.json in {user_dir}")
        return 0

    prs = [TargetPR.from_dict(d) for d in prs_data]
    imported = 0

    for pr in prs:
        pr_dir = pr.pr_dir(user_dir)
        if not os.path.isdir(pr_dir):
            continue

        status, enrichment_step = _infer_status(pr_dir)

        # Load all available data
        bq_events = _load_json(os.path.join(pr_dir, "02_fetch_events.json"))
        commits = _load_json(os.path.join(pr_dir, "03_commits_response.json"))
        reviews = _load_json(os.path.join(pr_dir, "04_reviews_response.json"))
        threads = _load_json(os.path.join(pr_dir, "05_review_threads_response.json"))
        details = _load_json(os.path.join(pr_dir, "06_commit_details_response.json"))
        assembled = _load_json(os.path.join(pr_dir, "assembled.json"))

        # Extract metadata from BQ events or assembled data
        pr_title = ""
        pr_author = None
        pr_created_at = None
        pr_merged = None

        if assembled:
            pr_title = assembled.get("pr_title", "")
            pr_author = assembled.get("pr_author")
            pr_created_at = assembled.get("pr_created_at")
            pr_merged = assembled.get("pr_merged")
        elif bq_events:
            # Extract from BQ events
            for event in bq_events:
                payload = event.get("payload", {})
                if event.get("type") == "PullRequestEvent":
                    pr_obj = payload.get("pull_request", {})
                    if not pr_title:
                        pr_title = pr_obj.get("title", "")
                    if pr_author is None:
                        pr_author = (pr_obj.get("user") or {}).get("login")
                    if pr_created_at is None:
                        pr_created_at = pr_obj.get("created_at")
                    if pr_merged is None and payload.get("action") == "closed":
                        pr_merged = bool(pr_obj.get("merged"))

        # Check if already exists
        existing = await repo.get_pr(chatbot_id, pr.repo_name, pr.pr_number)
        if existing is not None:
            logger.debug(f"PR {pr.repo_name}#{pr.pr_number} already in DB, skipping")
            continue

        # Insert PR
        await repo.insert_pr(
            chatbot_id=chatbot_id,
            repo_name=pr.repo_name,
            pr_number=pr.pr_number,
            pr_url=pr.pr_url,
            pr_title=pr_title,
            pr_author=pr_author,
            pr_created_at=pr_created_at,
            pr_merged=pr_merged,
            status=status,
            bq_events=bq_events,
        )

        # Get the inserted row ID
        row = await repo.get_pr(chatbot_id, pr.repo_name, pr.pr_number)
        if row is None:
            continue
        pr_id = row["id"]

        # Populate enrichment data
        if commits is not None:
            await repo.update_commits(pr_id, commits)
        if reviews is not None:
            await repo.update_reviews(pr_id, reviews)
        if threads is not None:
            await repo.update_threads(pr_id, threads)
        if details is not None:
            await repo.update_commit_details(pr_id, details)
        if assembled is not None:
            await repo.mark_assembled(pr_id, assembled)

        # Set final status and enrichment_step
        if enrichment_step == "done" and status == "enriched":
            await repo.mark_enrichment_done(pr_id)

        imported += 1
        if imported % 50 == 0:
            logger.info(f"  Imported {imported} PRs for {username}...")

    logger.info(f"Imported {imported} PRs for {username} (status based on files present)")
    return imported


async def import_all(output_dir: str, database_url: str, chatbot_filter: str | None = None) -> int:
    """Import all users from the output directory."""
    db = DBAdapter(database_url)
    await db.connect()
    try:
        await create_tables(db)

        total = 0
        if chatbot_filter:
            total = await import_user(db, output_dir, chatbot_filter)
        else:
            # Discover all user directories
            if not os.path.isdir(output_dir):
                logger.error(f"Output directory not found: {output_dir}")
                return 0
            for name in sorted(os.listdir(output_dir)):
                user_path = os.path.join(output_dir, name)
                if os.path.isdir(user_path) and os.path.exists(os.path.join(user_path, "01_find_prs.json")):
                    logger.info(f"Importing {name}...")
                    total += await import_user(db, output_dir, name)

        logger.info(f"Migration complete. Total PRs imported: {total}")
        return total
    finally:
        await db.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import filesystem PR data into database")
    parser.add_argument("--output-dir", default="output", help="Base output directory (default: output)")
    parser.add_argument("--chatbot", help="Only import a specific chatbot's data")
    parser.add_argument("--database-url", help="Override DATABASE_URL from .env")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    cfg = DBConfig()
    db_url = args.database_url or cfg.database_url

    asyncio.run(import_all(args.output_dir, db_url, chatbot_filter=args.chatbot))


if __name__ == "__main__":
    main()
