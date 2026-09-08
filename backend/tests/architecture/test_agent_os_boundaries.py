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


def test_removed_agent_service_has_no_importers() -> None:
    assert not (APP / "services" / "agent").exists()
    offenders = []
    for path in APP.rglob("*.py"):
        if any(name == "app.services.agent" or name.startswith("app.services.agent.") for name in _imports(path)):
            offenders.append(str(path.relative_to(APP)))
    assert offenders == []


def test_control_plane_does_not_import_execution_implementation() -> None:
    offenders = []
    for path in (APP / "control_plane").rglob("*.py"):
        if any(name.startswith("app.execution_plane") for name in _imports(path)):
            offenders.append(str(path.relative_to(APP)))
    assert offenders == []


def test_domain_and_gateway_are_framework_independent() -> None:
    forbidden = ("fastapi", "arq", "sqlalchemy", "app.api", "app.control_plane", "app.execution_plane")
    offenders: list[str] = []
    for root in (APP / "domains" / "pr_review", APP / "tool_gateway"):
        for path in root.rglob("*.py"):
            for name in _imports(path):
                if any(name == item or name.startswith(item + ".") for item in forbidden):
                    offenders.append(f"{path.relative_to(APP)} -> {name}")
    assert offenders == []
