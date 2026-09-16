"""Deprecated CLI compatibility for the PR node worker.

Production deployment uses :mod:`app.nodes.pr_review.worker`.  This module is
kept as a thin import surface for existing operators and third-party scripts.
"""

from app.nodes.pr_review.worker import (
    WorkerSettings,
    decode_task_payload,
    execute_agent_task_job,
    main,
    run_worker,
    shutdown_pr_node,
    startup_pr_node,
)

__all__ = [
    "WorkerSettings",
    "decode_task_payload",
    "execute_agent_task_job",
    "main",
    "run_worker",
    "shutdown_pr_node",
    "startup_pr_node",
]


if __name__ == "__main__":
    main()
