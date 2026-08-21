"""阶段 20 端到端 demo(spec 20 §6):陌生库自动索引 → 影响面分析 → 最小改动建议。

运行:python -m codesage.intel.demo
验证「最小改动 CodingAgent」核心链路:
1. 自动索引当前代码库(codebase-memory 知识图谱)
2. 影响面分析:查某函数的入站调用者
3. ponytail 阶梯生成最小改动建议
需 codebase-memory-mcp 已安装;未安装则打印降级提示。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from .minimal_change import MinimalChangeGuard
from .ponytail import register_ponytail
from .service import CodeIntelligenceService, discover_cbm_cli


async def _demo(project_dir: Path) -> int:
    cbm = discover_cbm_cli()
    if not cbm:
        print("codebase-memory-mcp 未安装。code intelligence 降级(引擎零变化)。")
        print("安装:https://github.com/DeusData/codebase-memory-mcp")
        return 1

    print(f"=== 阶段 20 demo:最小改动 CodingAgent ===")
    print(f"目标代码库: {project_dir}")

    print("\n[1/3] 自动索引代码库...")
    svc = CodeIntelligenceService(project_dir)
    ok = await svc.ensure_indexed()
    if not ok:
        print("索引失败,降级。")
        return 1
    print(f"  已索引,project_key={svc.project_key}")

    print("\n[2/3] 影响面分析(Engine.loop 的 AgentLoop 入站调用者)...")
    trace = await svc.trace("AgentLoop", "inbound")
    print(f"  Trace 有 {trace}")
    callers = int(trace.get("callers_total", 0)) if trace else 0
    print(f"  AgentLoop 有 {callers} 个入站调用者(改动它的影响面)")
    arch = await svc.get_architecture("structure")
    if arch:
        print(f"  库结构: {arch.get('total_nodes')} 节点 / {arch.get('total_edges')} 边")

    print("\n[3/3] ponytail 最小改动建议(引擎级约束)...")
    register_ponytail()
    guard = MinimalChangeGuard(svc)
    advice = await guard.guard("Edit", {"file_path": "codesage/engine/loop.py"})
    print(f"  约束层建议: {advice}")
    print(f"  无调用者建议: {await guard.guard('Write', {'file_path': 'new_mod.py'})}")

    print("\n=== demo 完成:自动索引 + 影响面分析 + 最小改动建议链路可用 ===")
    return 0


def main() -> int:
    # 默认索引 CodeSage 项目自身(或 CLI 参数指定)
    project = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    return asyncio.run(_demo(project))


if __name__ == "__main__":
    sys.exit(main())