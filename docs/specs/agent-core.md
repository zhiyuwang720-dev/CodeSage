# Spec: CodeSage Agent Core — 边界与设计理念

> 2026-08-05 方向调整:V1 最终目标 = **最稳定、无多余代码、只有最核心的 Agent Core**。
> 后续基于 Core 构建 Code CLI、多 Agent 编排等。本文档定义 Core 的边界与设计理念,借鉴当前代码、pi-agent-core、Claude Code 三源。

## 设计理念(四条)

1. **Core 是库,不是应用** —— 零 UI、零 CLI、零产品语义。任何前端(终端/服务/测试/多 Agent 编排)通过同一 API 驱动它。
2. **最稳定** —— API 面最小、行为可预测、无隐藏全局状态;所有异步以事件流呈现,消费者订阅而非轮询。
3. **无多余代码** —— 只保留「代理运行时」不可再分的最小集;权限、工具、LLM、UI 全部是 Core 之外的官方扩展包,通过扩展点接入。
4. **三源借鉴**:
   - **Claude Code**:每个错误都有恢复路径、每个恢复路径都有熔断、每个决定都幂等可重放
   - **pi-agent-core**:事件生命周期、工具三阶段管道(prepare/execute/finalize)、entry 链会话、StreamFn 错误入流
   - **当前代码**:权限决策链、审计、内部消息契约、非递归循环(已验证的成熟度)

## Core 边界

```
codesage/agent/                     # ← Agent Core(库)
  contract/        契约层
    message.py     内部消息契约(ContentBlock 体系, 现状 ai/types + core/messages 收敛)
    tool.py        Tool 契约(扁平对象) + ToolResult(含 terminate 语义)
    stream_fn.py   LLM 抽象: StreamFn(错误编码进流, 不 throw)
    event.py       AgentEvent 生命周期(agent/turn/message/tool 四级)
  runtime/         运行时
    loop.py        双层 while 循环(内层: 工具+steering; 外层: follow-up)
    agent.py       Agent 状态封装(state/队列/abort/subscribe/单飞)
    hooks.py       beforeToolCall/afterToolCall 管道 + {block:true} 拒绝语义
  session/         持久化
    store.py       Session(entry 链 + 操作日志 + torn-tail 恢复)
    jsonl.py       append-only JSONL + 原子写(fsync)
  __init__.py      最小公共 API

独立扩展包(不在 Core):
  codesage/llm/       LLM adapter(OpenAI/Anthropic, 实现 StreamFn)+ 模型指针/重试/取消
  codesage/perm/      权限引擎(决策链 + 审计, 实现 beforeToolCall hook)
  codesage/tools/     内置工具(Read/Write/Bash/..., 实现 Tool 契约)
  codesage/cli/       终端前端(消费 AgentEvent)
```

## 关键设计决策(三源选优)

| # | 决策 | 来源 | 理由 |
|---|---|---|---|
| 1 | **双层 while 循环** + 事件流 | pi | 显式迭代(无递归深度问题)+ steering/followUp 双队列;事件流让 UI/审计/持久化独立订阅 |
| 2 | **工具三阶段管道** prepare→execute→finalize | pi | 权限/校验在 prepare 串行完成(全部检查过才并发执行);finalize 提供结果改写口;`{block:true}` 让「拒绝」成为一等语义而非文本 |
| 3 | **内部消息契约 + convertToLlm 边界** | 我们 + pi | 循环内全程内部契约,只在 LLM 调用边界转换;上下文管理在契约层,provider 兼容在转换层 |
| 4 | **StreamFn 错误入流** | pi | 循环无需 try/catch 每个调用点;`stopReason: "error"/"aborted"` 统一处理 |
| 5 | **Session entry 链 + lane 指针** | pi | 追加式永不删除;fork/compaction/恢复都是插入 entry;操作日志支持「从中断点恢复」而非消息末尾 |
| 6 | **length 截断 → fail 全部工具** | pi | 截断可能产生残缺参数,全部重发比执行坏调用安全 |
| 7 | **工具 terminate(全批同意才停)** | pi | 工具侧可表达「该停了」 |
| 8 | **权限外置为扩展包** | pi + 我们现状 | Core 只留 beforeToolCall 钩子;我们成熟的决策链/审计迁移为 `codesage/perm` 官方扩展(安全底线不丢,Core 不臃肿) |
| 9 | **错误恢复阶梯 + 熔断** | CC | 可恢复错误先扣留 → 恢复阶梯 → 防死循环闸(作为 Core 的 loop 增强,迭代实现) |
| 10 | **幂等可重放** | CC | 工具结果落盘按 tool_use_id 确定性路径;会话决定记录可重建 |

## 明确不做(边界之外)

- UI/REPL/渲染(消费事件流的 cli 包)
- 权限决策逻辑(perm 包)
- 具体工具实现(tools 包)
- MCP/技能/记忆/多 Agent(后续阶段,全部基于 Core 扩展点)
- 富 TUI、daemon、协议层

## Core 验收标准

- [ ] 零 UI/CLI 依赖:core 包可被任意前端驱动
- [ ] 事件流完整:agent/turn/message/tool 四级生命周期,失败也是完整序列
- [ ] 工具管道:prepare 拒绝以 block 语义生效;finalize 可改写结果
- [ ] 会话:entry 链 + 操作日志,崩溃后可从中断点恢复
- [ ] 无隐藏全局状态;所有可配置项经构造参数注入
- [ ] 现有 382 测试语义保留(权限/审计/契约迁至扩展包后仍全绿)
