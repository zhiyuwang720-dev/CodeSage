from __future__ import annotations

from app.contracts.tools import RuntimeTool
from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.services.input_builder import ReviewSnapshot
from .base import DeepToolContext
from .code_search import CodeSearchTool
from .file_find import FileFindTool
from .file_read import FileReadTool
from .file_read_diff import FileReadDiffTool


def build_review_tools(*, repo_path: str, head_commit: str, snapshot: ReviewSnapshot, config: DeepReviewConfig) -> list[RuntimeTool]:
    context = DeepToolContext(
        repo_path=repo_path,
        head_commit=head_commit,
        snapshot=snapshot,
        config=config,
    )
    return [
        FileReadTool(context),
        FileReadDiffTool(context),
        FileFindTool(context),
        CodeSearchTool(context),
    ]

