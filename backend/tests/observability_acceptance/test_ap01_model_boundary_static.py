"""AP01（L0/静态）：唯一模型出口、依赖规则与旧实现零引用（P01,P03,P11）。"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
TESTS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]

DELETED_MODULES = [
    "app/execution_plane/models/adapters",
    "app/execution_plane/models/protocols",
    "app/execution_plane/models/base_adapter.py",
    "app/execution_plane/models/factory.py",
    "app/execution_plane/models/retry.py",
    "app/execution_plane/models/prompt_cache.py",
    "app/execution_plane/models/tokenizer.py",
    "app/execution_plane/models/memory_compressor.py",
    "app/infrastructure/observability/provider_requests.py",
]

LEGACY_SYMBOLS = (
    "LLMFactory",
    "BaseLLMAdapter",
    "LiteLLMAdapter",
    "AnthropicAdapter",
    "BaiduAdapter",
    "MinimaxAdapter",
    "DoubaoAdapter",
    "GeminiNativeAdapter",
    "OpenAIResponsesAdapter",
    "observe_provider_request",
    "prompt_cache_manager",
    "MemoryCompressor",
    "TokenEstimator",
    "retry_with_backoff",
    "LLM_RETRY_CONFIG",
)


def _python_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*.py") if "__pycache__" not in path.parts]


def test_ap01_legacy_modules_are_deleted() -> None:
    remaining = [relative for relative in DELETED_MODULES if (APP_ROOT.parent / relative).exists()]
    assert remaining == []


def test_ap01_no_production_reference_to_legacy_symbols() -> None:
    offenders: list[str] = []
    for path in _python_files(APP_ROOT):
        text = path.read_text(encoding="utf-8")
        for symbol in LEGACY_SYMBOLS:
            if symbol in text:
                offenders.append(f"{path.relative_to(APP_ROOT.parent).as_posix()}:{symbol}")
    assert offenders == []


def test_ap01_single_production_sdk_call_site() -> None:
    call_sites: list[str] = []
    for path in _python_files(APP_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                value = node.func.value
                if isinstance(value, ast.Name) and value.id == "litellm" and node.func.attr in {
                    "acompletion",
                    "completion",
                }:
                    call_sites.append(f"{path.relative_to(APP_ROOT.parent).as_posix()}:{node.lineno}:{node.func.attr}")
    assert len(call_sites) == 1, call_sites
    assert call_sites[0].startswith("app/execution_plane/models/client.py")


def test_ap01_no_global_litellm_configuration_mutation_in_production() -> None:
    forbidden = ("litellm.cache", "litellm.drop_params", "litellm.model", "litellm.api_base", "litellm.api_key")
    offenders: list[str] = []
    for path in _python_files(APP_ROOT):
        text = path.read_text(encoding="utf-8")
        for attribute in forbidden:
            if f"{attribute} =" in text or f"{attribute}=" in text:
                offenders.append(f"{path.relative_to(APP_ROOT.parent).as_posix()}:{attribute}")
    assert offenders == []


def test_ap01_no_provider_protocol_dispatch_in_client() -> None:
    client = (APP_ROOT / "execution_plane" / "models" / "client.py").read_text(encoding="utf-8")
    assert "httpx" not in client
    assert "aiohttp" not in client
    for marker in ("if provider ==", "elif provider ==", "if self.config.provider =="):
        assert marker not in client


def test_ap01_call_graph_evidence(acceptance_artifact_root: Path) -> None:
    service_callers = sorted(
        path.relative_to(APP_ROOT.parent).as_posix()
        for path in _python_files(APP_ROOT)
        if "LLMService(" in path.read_text(encoding="utf-8")
    )
    payload = {
        "requirement": "P01,P03,P11",
        "acceptance": "AP01",
        "production_sdk_call_sites": ["app/execution_plane/models/client.py:litellm.acompletion"],
        "llm_service_construction_sites": service_callers,
        "deleted_modules": DELETED_MODULES,
        "legacy_symbols_checked": list(LEGACY_SYMBOLS),
        "commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=10
        ).stdout.strip(),
    }
    target = acceptance_artifact_root / "evidence" / "ap01" / "callers.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    assert payload["commit"]
    assert service_callers
