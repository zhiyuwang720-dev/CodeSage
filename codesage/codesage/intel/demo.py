"""阶段 20 端到端 demo(spec 20 §6):陌生库自动索引 → 影响面分析 → 最小改动建议。

运行:python -m codesage.intel.demo
验证「最小改动 CodingAgent」核心链路:
1. 自动索引当前代码库(codebase-memory 知识图谱,后台线程不阻塞)
2. 影响面分析:structuredContent 解包(数字为 int)+ 短名歧义识别
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

    print("=== 阶段 20 demo:最小改动 CodingAgent ===")
    print(f"目标代码库: {project_dir}")

    print("\n[1/4] 自动索引代码库(后台线程,不阻塞启动)...")
    svc = CodeIntelligenceService(project_dir)
    svc.start_background_index()
    if not await svc.wait_ready(timeout_s=120):
        print("索引未就绪,降级。")
        return 1
    print(f"  已索引,project_key={svc.project_key}")

    print("\n[2/4] 影响面分析(trace_path format=json,structuredContent 解包)...")
    trace = await svc.trace("AgentLoop", "inbound")
    if trace:
        n = trace.get("callers_total")
        print(f"  AgentLoop 入站调用者: {n}(int={isinstance(n, int)} — 真结构化,非文本猜测)")
    impact = await svc.impact_of_change("run")
    if impact:
        print(f"  短名 'run' → status={impact.get('status')}(修正:歧义不再误判为 0 调用者)")
        if impact.get("status") == "ambiguous":
            names = [s.get("qualified_name") for s in impact.get("suggestions", [])][:3]
            print(f"    候选: {names}")

    print("\n[3/4] 库结构概要...")
    arch = await svc.get_architecture("structure")
    if arch:
        print(f"  {arch.get('total_nodes')} 节点 / {arch.get('total_edges')} 边")

    print("\n[4/4] ponytail 最小改动建议(引擎级约束层)...")
    register_ponytail()
    guard = MinimalChangeGuard(svc)
    advice = await guard.guard("Edit", {"file_path": "codesage/engine/loop.py"})
    text = advice.content if hasattr(advice, "content") else advice
    print(f"  约束层建议: {text}")
    print("  提示:同一目标重试将放行(拦一次语义)")

    print("\n=== demo 完成:自动索引 + 影响面分析 + 最小改动建议链路可用 ===")
    return 0


def main() -> int:
    # 默认索引 CodeSage 项目自身(或 CLI 参数指定)
    project = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    return asyncio.run(_demo(project))


if __name__ == "__main__":
    sys.exit(main())
