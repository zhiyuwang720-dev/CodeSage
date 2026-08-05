"""Terminal permission prompt: y / n / remember (writes settings.local.json)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..permissions import PermissionDecision, save_approval
from .render import CYAN, RESET, USE_COLOR, _c


async def request_permission(
    decision: PermissionDecision,
    tool: Any,
    tool_input: dict[str, Any],
    *,
    local_settings_path: Path,
    message_sink=None,
) -> bool:
    """Ask the user; "remember" persists an allow rule for this tool."""
    reason = decision.reason or f"{tool.name} needs approval"
    print(_c(f"\n[权限请求] {reason}", CYAN))
    while True:
        answer = await asyncio.to_thread(
            input, "(y)es / (n)o / (r)emember: "
        )
        answer = answer.strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no", ""):
            return False
        if answer in ("r", "remember"):
            save_approval(local_settings_path, tool.name, tool.name)
            print(_c(f"已记住:允许 {tool.name}(写入 settings.local.json)", CYAN))
            return True
        print("输入 y / n / r")
