# pi-agent-core 分析(2026-08-05)

> 来源:深读代理对照 `/e/Mac/CodeSage/pi/packages/agent`(42 文件,11081 行)与 CodeSage V1。pi-agent-core 是 Mario Zechner(Kode 作者)的重写版代理运行时。

## 核心抽象

### 1. AgentMessage — 应用状态与模型上下文的分离(重点记录)

**设计**:`AgentMessage = Message | CustomAgentMessages[...]`(types.ts:310-319)。循环内部全程使用 AgentMessage(LLM 三类消息 user/assistant/toolResult + 应用自定义消息),**只在 LLM 调用边界转换**为 `Message[]`(agent-loop.ts 文件头注释),转换点是 `config.convertToLlm`。

**为什么好**:
- 应用状态(bash 执行记录、分支摘要、compaction 摘要、自定义事件)与模型上下文**物理分离**:模型只看到 convertToLlm 转换后的内容,应用状态不进模型上下文(除非显式注入)
- 转换边界单一:上下文裁剪/注入在 AgentMessage 层做(transformContext),provider 兼容在转换层做 —— 两层各管各的
- 自定义消息通过 TS 声明合并扩展,无字符串 hack

**对我们的启示**:我们的 SessionMessage 是单一类型(role/content + 元数据),工具状态、审计信息都塞在 content 或 meta 里。pi 的「自定义消息类型 + 边界转换」更干净 —— 阶段 12(会话生命周期)或 19(插件)可借鉴:应用事件(审计/进度/分支摘要)作为独立消息类型进会话,转换时过滤。

### 2. 树状会话结构(重点记录)

**设计**:会话以 **entry 链 + lane 指针**存储(harness/session/),支持**树状分支**:

- **Entry**(types.ts:14-74):消息/模型变更/思考级别/活动工具/compaction/branch_summary/custom 等条目,追加式,**永不删除**;compaction 也是「插入一个 entry」
- **Lane 指针**(types.ts:150-212):指向 leaf 的指针,**分支/fork 是追加一个新的 lane 指针**,天然可回滚、可恢复
- **Fork 语义**(session.ts:338-351):`{scope: "branch"}`(从 entryId 分支)或 `{scope: "tree"}`(整树)
- **JSONL v4 存储**(jsonl.ts):每 mutation 一行带全局 seq;加载校验连续性,**torn-tail 截断修复**(jsonl.ts:237-268);SessionState 纯内存重放(state.ts)

**产品形态(用户补充,记录)**:会话以树状结构存储,可用于 **/tree 导航至任何先前位置并从那里继续**;所有分支保存在**单个文件**中;可按**消息类型筛选**;条目可标记**书签**。

**为什么好**:
- 「分支 = 追加一个指针」:fork/compaction/回滚都不破坏历史,单文件承载整个决策树
- 崩溃恢复:操作日志(Record: operation_started/tool_started/step_attempt)让 `--continue` 可从「中断点」而非「消息末尾」恢复(findOpenOperations)
- 书签 + 类型筛选:导航成本低,长会话可回溯任何决策点

**对我们的启示**:我们的 Session 是扁平消息行(阶段 04),`--continue` 只是重放。树状会话是阶段 12(会话生命周期)的重要升级方向:消息加 parent 链、分支指针、单文件多分支。

### 3. 其他核心抽象

| 抽象 | 设计 | 位置 |
|---|---|---|
| AgentTool | 扁平对象:label + TypeBox schema + prepareArguments? + execute(signal, onUpdate) + executionMode(单工具覆盖全局并行策略) | types.ts:380-403 |
| Agent(有状态封装) | state(systemPrompt/model/thinkingLevel/tools/messages/pendingToolCalls)+ steering/followUp 双队列 + subscribe 事件 + abort/waitForIdle;run 失败也转完整事件序列 | agent.ts:173-588 |
| agent-loop(无状态纯循环) | 双层 while:内层处理工具调用 + steering,外层轮询 followUp;每轮 streamAssistantResponse → 过滤 toolCall → 三阶段工具执行 → turn_end → prepareNextTurn → shouldStopAfterTurn | agent-loop.ts:155-275 |
| StreamFn | 模型调用唯一抽象:不 throw,错误编码进事件流(stopReason: "error"|"aborted") | stream-fn.ts |
| 工具三阶段 | prepare(参数+校验+权限钩子 `{block:true}`)→ execute(并行,结果按源顺序回报)→ finalize(结果改写) | agent-loop.ts:600-754 |
| 事件流 | agent_start/end、turn_start/end、message_start/update/end、tool_execution_start/update/end | types.ts:422-437 |

## 关键设计决策

1. **事件流驱动 + 订阅者模型** — UI/扩展/持久化各自独立订阅,不互相耦合
2. **双层循环 + steering/followUp 队列** — 区分「运行中插话」与「结束后追问」,QueueMode 控制注入条数
3. **工具三阶段** — 权限/校验在 prepare 串行完成(全部检查过才并发),结果改写集中在 finalize
4. **beforeToolCall `{block:true}`** — 权限拒绝以错误 toolResult 返回模型,模型可自愈
5. **parallel 执行但结果按源顺序回报** — 并发提速 + 会话顺序稳定
6. **只有 batch 内所有工具 terminate 才提前停** — 避免单一工具误判终止
7. **stopReason === "length" 时 fail 全部工具调用** — 截断可能产生残缺参数,全部重发比执行坏调用安全
8. **convertToLlm / transformContext 双边界** — 上下文管理在 AgentMessage 层,provider 兼容在转换层
9. **StreamFn 错误入流而非 throw** — 循环无需 try/catch 每个调用点
10. **会话 entry 链 + lane 指针** — 分支/fork/compaction 都是追加,天然可回滚
11. **compaction 的 token 估算 usage 优先** — 用最近一次真实 assistant usage 代替纯字符猜测
12. **cut point 不拆 turn** — 拆了则前缀单独摘要,模型不读半截 turn
13. **Result<T,E> + TaggedError + 稳定错误码** — FS/shell 期望内失败不 throw
14. **运行失败转成完整事件序列** — 事件消费者不需要感知「异常路径」

## 与 CodeSage V1 对比

### pi 更强(优点)

| 高 | pi 做法 | 我们 |
|---|---|---|
| 工具生命周期事件 | tool_execution_start/update/end 三事件 | 裸 yield 消息,UI 拿不到工具级时序 |
| beforeToolCall/afterToolCall 三阶段 | 拒绝是一等语义({block:true})+ 结果改写口 | pre/post_hook 半成品,拒绝只是文本 ToolResult |
| 工具 terminate 语义 | 全批同意才停 | 无工具侧终止通道 |
| length 截断 fail 全部工具 | 截断时全部 tool_call 标错重发 | 已解析出完整参数的调用照常执行 —— 残缺参数风险 |
| 持久化会话 + 操作日志 | Entry/Record 分离 + findOpenOperations 恢复 | --continue 只是重放,不知「上次跑到哪」 |
| 结构化 compaction | usage 优先估算 + turn 边界 + split-turn 前缀摘要 | 完全无 compaction |
| steering/followUp | 双队列 + 排空模式 | REPL 同步读一行跑一轮 |
| 运行失败完整事件序列 | handleRunFailure 保证事件完整 | abort 路径只发一条 meta 消息 |

### 我们更强(缺点)

| 高 | 我们 | pi |
|---|---|---|
| 权限决策链 | 10 步链 + bash 静态分析 + 写保护 + 工作目录 + 模式 | 完全缺席,只有 beforeToolCall 钩子 |
| 审计 | 每决策 ToolAuditEvent | 无内置审计 |
| sibling 失败保守传播 | 未启动兄弟 void(写场景安全) | 并行不取消兄弟 |
| 默认串行并发 | is_concurrency_safe 显式 True 才并行(fail-closed) | 默认 parallel |
| 通用结果 spill | >100KB 落盘 + 模型看指针 | 只对 bash 截断 |
| turn/budget 内置 | 引擎内 max_turns/max_budget | 依赖应用层 |
| 双 adapter + 指针回退 | 开箱即用 | 模型目录在应用层 |
| 内聚 | engine/ 一个包通读 | harness 是 stub,真实现散在 58000 行 |

## 可借鉴设计(已写入 tasks/todo.md,PI-01~10)

| # | 借鉴点 | 落点 | 价值 |
|---|---|---|---|
| PI-01 | 工具执行生命周期事件(执行中状态/流式输出) | engine + cli/render | 高 |
| PI-02 | beforeToolCall {block:true} + 三阶段管道(拒绝一等语义 + afterToolCall 结果改写口) | tool_queue + permissions | 高 |
| PI-03 | stopReason=="length" 时 fail 全部工具调用(防残缺参数执行) | ai/client collect + engine | 高 |
| PI-04 | 工具结果 terminate(全批同意才停) | tools/base ToolResult + engine | 高 |
| PI-05 | 结构化 compaction 管线(usage 优先估算 + turn 边界 + split-turn 前缀摘要) | 新 engine/compaction.py | 高 |
| PI-06 | steering/followUp 双队列(运行中插话/结束后追问) | cli/repl + engine | 中 |
| PI-07 | 会话操作日志 + findOpenOperations 恢复(--continue 从中断点恢复) | core/session | 中 |
| PI-08 | 模型/思考级别/活动工具作为 entry(会话自描述) | core/session | 中 |
| PI-09 | 树状会话(entry 链 + lane 指针 + /tree 导航 + 书签 + 类型筛选) | core/session(阶段 12 升级) | 高(长期) |
| PI-10 | AgentMessage 自定义消息类型 + convertToLlm 边界(应用状态与模型上下文分离) | core/messages(阶段 12/19) | 中(长期) |
| PI-11 | 失败也走完整事件序列 | engine/loop | 低 |
| PI-12 | Result + 稳定错误码(期望内失败不 throw) | tools 内部约定 | 低 |

## 结论

pi 整体设计水平高于我们(事件模型、工具管道、持久化会话、compaction 都成熟),但权限安全完全缺席 —— 那是我们的主场。最值得抄:①工具三阶段管道 + 生命周期事件;②结构化 compaction;③树状会话(长期)。
