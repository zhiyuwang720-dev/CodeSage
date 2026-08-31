from __future__ import annotations

from typing import Any, Dict, Iterable, List


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _string_list(values: Any) -> List[str]:
    result: List[str] = []
    for item in _as_list(values):
        if isinstance(item, str):
            text = item.strip()
            if text and text not in result:
                result.append(text)
    return result


def _merge_string_lists(*groups: Iterable[str]) -> List[str]:
    merged: List[str] = []
    for group in groups:
        for item in group:
            text = str(item).strip()
            if text and text not in merged:
                merged.append(text)
    return merged


def _normalize_project_structure(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = _as_dict(payload.get("project_structure"))
    return {
        "key_directories": _merge_string_lists(
            _string_list(raw.get("key_directories")),
            _string_list(raw.get("directories")),
        ),
        "key_files": _merge_string_lists(
            _string_list(raw.get("key_files")),
            _string_list(raw.get("files")),
        ),
        "monorepo_layout": str(raw.get("monorepo_layout", "") or "").strip(),
    }


def _normalize_project_profile(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = _as_dict(payload.get("project_profile") or payload.get("tech_stack"))
    return {
        "languages": _string_list(raw.get("languages")),
        "frameworks": _string_list(raw.get("frameworks")),
        "databases": _string_list(raw.get("databases")),
        "package_managers": _string_list(raw.get("package_managers")),
        "runtime_indicators": _string_list(raw.get("runtime_indicators")),
    }


def _normalize_recommended_scanners(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = _as_dict(payload.get("recommended_scanners") or payload.get("recommended_tools"))
    return {
        "must_use": _string_list(raw.get("must_use")),
        "optional": _merge_string_lists(
            _string_list(raw.get("optional")),
            _string_list(raw.get("recommended")),
        ),
        "reason": str(raw.get("reason", "") or "").strip(),
    }


def _normalize_audit_targets(payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    raw = _as_dict(payload.get("audit_targets"))
    target_files = _merge_string_lists(
        _string_list(raw.get("target_files")),
        _string_list(config.get("target_files")),
    )
    exclude_patterns = _merge_string_lists(
        _string_list(raw.get("exclude_patterns")),
        _string_list(config.get("exclude_patterns")),
    )
    return {
        "target_files": target_files,
        "exclude_patterns": exclude_patterns,
    }


def normalize_recon_payload(payload: Dict[str, Any] | None, *, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    raw = _as_dict(payload)
    config = _as_dict(config)

    priority_paths = _merge_string_lists(
        _string_list(raw.get("priority_paths")),
        _string_list(raw.get("high_risk_areas")),
    )

    entry_points = [item for item in _as_list(raw.get("entry_points")) if isinstance(item, dict)]

    return {
        "project_profile": _normalize_project_profile(raw),
        "project_structure": _normalize_project_structure(raw),
        "entry_points": entry_points,
        "priority_paths": priority_paths,
        "audit_targets": _normalize_audit_targets(raw, config),
        "recommended_scanners": _normalize_recommended_scanners(raw),
        "summary": str(raw.get("summary", "") or "").strip(),
    }
