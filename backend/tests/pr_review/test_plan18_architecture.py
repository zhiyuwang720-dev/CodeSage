import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "app"


def test_services_and_worker_do_not_import_api_layer():
    offenders = []
    for root in (ROOT / "services", ROOT / "worker"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(name == "app.api" or name.startswith("app.api.") for name in names):
                    offenders.append(f"{path.relative_to(ROOT.parent)}:{node.lineno}")
    assert offenders == []


def test_default_execution_path_targets_service_use_case():
    source = (ROOT / "services" / "pr_review" / "execution.py").read_text(encoding="utf-8")
    assert "app.api" not in source
    assert "execute_review_use_case" in source
