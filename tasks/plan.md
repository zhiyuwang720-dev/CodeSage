# CodeSage 实施计划

> 依据:`docs/specs/codesage.md`(已批准)。Phase 2:组件依赖、顺序、风险、检查点。

## 依赖图

```
01 config ──→ 02 ai ──→ 03 tools ──→ 04 core ──→ 05 permissions ──→ 06 engine ──→ 07 cli (V1 主线)
                 │                    │               │                  │
                 │                    ├─→ 08 context ─┘                  └─→ 13 subagents
                 │                    ├─→ 09 hooks ──→(汇入 06 挂接点,可后补)
                 │                    ├─→ 10 compact(还需 02 ai)
                 │                    ├─→ 11 tasks
                 │                    ├─→ 12 session
                 │                    └─→ 17 memory(还需 01 config)
03 tools ──→ 14 skills(还需 06)
03 tools ──→ 15 mcp(还需 04)
05 permissions + 03 tools ──→ 16 bash-safety
02 ai ──→ 18 multimodel
全部 ──→ 19 plugins(收尾)
```

主线串行(V1):01→02→03→04→05→06→07,依赖强制,无并行。
V2+(08–18):依赖上互不阻塞,可任意顺序推进;单开发者按编号顺序即可,某阶段受阻不阻塞后续。

## 关键风险与缓解

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | **Python 递归深度**:Kode 用递归 async generator 实现主循环,Python 默认 recursionlimit 1000,约千轮对话即 RecursionError | 高 —— 阶段 06 核心 | 阶段 06 规格中评估两种方案:a) `sys.setrecursionlimit`(每轮栈帧大,风险仍存);b) 显式迭代器 + 内部消息队列(推荐,无深度问题)。**阶段 06 分支上必须做深度压力测试(>2000 轮模拟)** |
| R2 | LLM 流式中断语义:Python asyncio 下流中 abort 的边界 | 中 | httpx.AsyncClient 全链路 AbortSignal(阶段 02 就接入),阶段 06 用 VCR 回放测中断 |
| R3 | 文件锁跨平台(Windows 上文件锁语义不同) | 中 | 原子写(tmp+rename)为主,锁仅用于会话/记忆并发,Windows 用 msvcrt 或退化为 O_EXCL 试探(阶段 04/17 实现时验证) |
| R4 | 沙箱在 Windows 不可用 | 低(已降级) | 阶段 16 只做守卫 + LLM 闸门,沙箱计划接口化 + 文档化,Linux 预留 |
| R5 | 阶段范围失控(「模块做到最大」无上限) | 中 | 每阶段规格写死完成标准(对照保留清单),完成标准满足即合并不加戏 |
| R6 | 现有 backend/ 代码干扰新设计 | 低 | 新目录 `codesage/` 物理隔离;只在阶段 02/04 作参考素材读取 |
| R7 | 递归 generator 无法尾递归 → 深轮次栈帧累积 | 中 | 同 R1,方案 b) 一并解决 |

## 验证检查点

1. **每阶段完成门**:该阶段单测全绿 + `docs/modules/0N-*.md` 存在 + 合并 master
2. **V1 验收(阶段 07)**:REPL 端到端「创建 docs/hello.md」类任务;权限 allow/ask/deny 三态生效;审计钩子有记录;`pytest tests/ -q` 全绿
3. **阶段 06 专项**:>2000 轮循环压力测试(防 R1)
4. **阶段 19 完成门**:同类别模块 ≥2 实现,注册层切换零代码侵入

## 实施方式

- 每阶段:切 `feat/0N-xxx` 分支 → 写阶段规格(六项核心区 + 完成标准)→ 实现 + 单测 → 文档 → 合并 master
- 阶段 01 分支自 master;后续分支自 master(各阶段独立,避免长链分支)
- 每阶段合并前:目标分支由用户确认
