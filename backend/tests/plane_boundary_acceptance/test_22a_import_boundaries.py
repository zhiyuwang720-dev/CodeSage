from __future__ import annotations

import ast
from pathlib import Path


APP = Path(__file__).resolve().parents[2] / "app"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _violations(
    root: Path, forbidden: tuple[str, ...], *, ignored: set[str] | None = None
):
    found = []
    for path in root.rglob("*.py"):
        if path.name in (ignored or set()):
            continue
        for imported in _imports(path):
            if imported.startswith(forbidden):
                found.append((path.relative_to(APP).as_posix(), imported))
    return found


def test_22a_t02_control_plane_does_not_depend_on_nodes_or_runtime():
    assert not _violations(
        APP / "control_plane",
        ("app.nodes", "app.node_runtime", "litellm"),
        ignored={"results.py", "review_inputs.py", "review_policy.py"},
    )


def test_22a_t02_node_runtime_is_product_neutral():
    assert not _violations(APP / "node_runtime", ("app.nodes.pr_review", "app.api"))


def test_22a_t02_pr_domain_has_no_framework_or_persistence_dependency():
    assert not _violations(
        APP / "nodes" / "pr_review" / "domain", ("sqlalchemy", "arq", "fastapi")
    )


def test_22a_t02_internal_code_uses_owner_model_paths():
    deprecated = (
        "app.models.audit_session",
        "app.models.checkpoint",
        "app.models.report_template",
        "app.models.prompt_template",
        "app.models.audit_rule",
        "app.models.review_execution",
    )
    assert not _violations(
        APP,
        deprecated,
        ignored={
            "audit_session.py",
            "checkpoint.py",
            "report_template.py",
            "prompt_template.py",
            "audit_rule.py",
            "review_execution.py",
        },
    )
