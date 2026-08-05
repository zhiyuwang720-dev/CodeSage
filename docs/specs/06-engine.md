# Spec: 阶段 06 — 引擎主循环

> 分支:`feat/06-engine`。依据主规格 `docs/specs/codesage.md`(阶段 06)。
> 这是 Agent Runtime 本体(架构决策:Agent Runtime 职责全部在此)。

## Objective

把 01–05 串起来:一次 agent 运行的完整循环(消息 → 模型 → 工具调用 → 结果 → 下一轮),产出 `SessionMessage` 流。**R1 风险在此解决**:Kode 用递归 async generator 做循环,Python 递归深度限制(默认 1000)会在千轮对话后崩溃 —— 本阶段用显式 while 迭代(消息列表在循环内维护),并做 >2000 轮压力测试。

## 对照保留清单

- #1 主循环:Message 流是唯一信息通道;终止条件(终答/max_turns/max_budget/abort)
- #2 工具失败不抛异常 → error tool_result 交给模型自愈;唯一硬异常 max_turns/max_budget
- #3 ToolUseQueue:is_concurrency_safe 并行、非安全工具 = 顺序屏障;一个工具出错 → sibling 收 `<tool_use_error>`
- #4 AbortSignal 三检查点(LLM 调用后 / 工具队列 / hooks 挂接点)
- #5/#6 权限引擎接入(ask 决策通过 request_permission 回调;阶段 07 接 UI,默认拒绝)

## 范围

**做**:
1. `AgentLoop`:while 迭代主循环(非递归,解决 R1),产出 SessionMessage 流
2. `ToolUseQueue`:并发调度(安全工具并行、非安全屏障、sibling error)
3. 错误转 tool_result;MaxTurns/MaxBudget 硬限制
4. abort(三检查点)+ 中断消息(is_meta)
5. hooks 挂接点(pre/post tool use,阶段 09 实现)
6. system prompt 骨架(阶段 08 完整化)
7. >2000 轮压力测试

**不做**:ProgressMessage 瞬态(07);完整 system prompt 分层(08);hooks 实现(09);压缩(10);子代理 Task 工具(13);Bash 命令级权限(16)。

## 项目结构(本阶段新建)

```
codesage/codesage/engine/
  __init__.py
  loop.py           # AgentLoop 主循环
  tool_queue.py     # ToolUseQueue
  system_prompt.py  # 骨架
tests/engine/
  test_loop.py
  test_tool_queue.py
  test_pressure.py  # >2000 轮
```

## Commands

```bash
pytest tests/engine/ -q
pytest tests/engine/test_pressure.py -q   # 压力测试(5000 轮)
```

## Testing Strategy

- 循环终止:终答/超轮数/超预算/abort 四类
- 错误自愈:工具抛异常 → error tool_result → 模型收到
- 队列:并发屏障、sibling error
- 压力:5000 轮迭代无 RecursionError、无内存泄漏告警
- 全程 mock LLM(假 client 返回固定序列),零网络

## Boundaries

- **Always**: 循环必须能在任意轮数下运行(无递归);工具异常必须进 error tool_result;每轮 LLM 调用前检查 abort
- **Ask first**: 改循环终止语义;加全局状态
- **Never**: 在循环内做权限决策(引擎只调用);吞掉 MaxTurns 之外的硬错误

## Success Criteria

- [ ] 四类终止 + 错误自愈 + 中断全测
- [ ] 5000 轮压力测试通过(无 RecursionError)
- [ ] ToolUseQueue 屏障语义正确
- [ ] 全量单测绿
