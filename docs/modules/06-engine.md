# 阶段 06 — 引擎主循环理解文档

> 分支 `feat/06-engine`,规格见 `docs/specs/06-engine.md`。
> 这是 Agent Runtime 本体(架构决策:Agent Runtime 职责全部在此,见 `docs/specs/codesage.md` 路线图注释)。

## 模块职责

把 01–05 串起来:一次 agent 运行的完整循环。**它是 harness 的心脏** —— 模型、工具、权限、会话在这一层交汇。它产出 `SessionMessage` 流,消费方(CLI/测试/子代理)对内部机制无感知。

## 核心决策:R1 的解法 —— 显式 while,不用递归

Kode 的主循环是**递归 async generator**(`messagePipelineCore` 在工具结果收齐后 `yield* messagePipelineCore(...)` 调用自身)。Python 复制这个写法会在 ~1000 轮(默认 recursionlimit)后 `RecursionError` —— 这就是计划里的 R1 风险。

**解法**:消息列表在循环内增长,`while True` 显式迭代:

```python
messages = [first_user_message]
while True:
    if turn >= max_turns: yield finish; return
    if abort.is_set(): yield interrupt; return
    assistant = await self._ask_model(messages)   # normalize → LLM stream → collect
    yield assistant; messages.append(assistant)
    if not tool_uses: return                       # 终答
    results = await ToolUseQueue(...).run()        # 并发屏障 + 权限
    tool_round = user_message(results)
    yield tool_round; messages.append(tool_round)
```

- **同样的透明消息流**(yield 即消费,阶段 07 UI 直接接)
- **零栈增长**:2001 轮压力测试通过(16 秒)——
  - 递归版本会在 1000 轮 RecursionError(测试设计为「超过默认限制 2 倍」)
  - 每轮重新 normalize 增长的消息列表是 O(n²),阶段 10 压缩解决真实场景

## 关键设计决策

### 1. 四类终止条件(保留清单 #1)

| 终止 | 产物 |
|---|---|
| 模型终答(无 tool_use) | 正常结束 |
| max_turns / max_budget | `is_meta` 说明消息 |
| abort(三检查点) | `(interrupted by user)` is_meta 消息 |
| 不可恢复 LLM 错误 | `is_error` assistant 消息(不崩溃) |

### 2. 错误自愈(保留清单 #2)

- 工具抛异常/权限拒绝/未知工具 → **全部转成 `is_error` tool_result**,模型看到失败自己调整 —— 循环永不因工具失败而崩
- 唯一硬性停止:max_turns/max_budget

### 3. ToolUseQueue(保留清单 #3)

- `is_concurrency_safe` 工具同批并行(gather);非安全工具 = 屏障(独占一批)
- **一个工具失败 → 同批全部 sibling 作废 + 后续队列全部作废**,统一收 `<tool_use_error>Sibling tool call errored</tool_use_error>` —— 模型必须处理,而不是假装成功
- 未知工具名:预设 error 结果,不进执行(有测试)

### 4. 中断:abort 事件(保留清单 #4)

`asyncio.Event`,检查点三处:循环顶部、LLM 调用后、工具批前。中断产物是 is_meta 消息(normalize 时被过滤,不进模型上下文)。hooks 挂接点是第四个(阶段 09)。

### 5. 权限接入(保留清单 #5/#6)

`permission_check` 回调挂在 ToolUseQueue:引擎评估 → deny 直接拒绝(模型收到 Permission denied tool_result)→ ask 走 `request_permission` 回调(阶段 07 接 UI,本阶段默认拒绝 —— 安全默认,绝不默认放行)。

### 6. 持久化副作用

`session` 参数可选:每条产出消息 append(阶段 04 的 JSONL)。循环本体与持久化解耦(persistSession=False 可整体关闭,测试即如此)。

## 与 Kode 的对照

| CodeSage | Kode | 差异 |
|---|---|---|
| while 迭代 | 递归 generator | **有意不同**:R1 风险,见上;压力测试证明等价 |
| collect() 非流式内部拼装 | 流式事件直接消费 | 阶段 07 升级为边收边渲染(接口已支持) |
| abort 三检查点 | 三检查点 + hooks | hooks 09 补 |
| 无 micro/auto compact | 两级压缩在循环内 | 阶段 10 |
| 无 goal/turn 状态机 | goal 续跑等 | 阶段 11/18 |

## 已知简化(ponytail)

- 每轮 `tools.specs()` 重建 ToolSpec 列表(7 个对象,便宜;MCP 阶段 15 后数量上来再缓存)
- 消息列表 O(n²) 重新归一化 —— 阶段 10 压缩是正解
- 无 ProgressMessage(07 UI);无 thinking-only 恢复重试(Kode 3 次,需要时补)

## 完成标准(对照规格)

- [x] 四类终止 + 错误自愈 + 中断 + 权限拒绝全测(13 项)
- [x] 2001 轮压力测试通过(无 RecursionError)
- [x] ToolUseQueue 屏障 + sibling 作废语义
- [x] 156 项全量单测绿

## 阶段衔接

- 阶段 07(CLI):消费 SessionMessage 流 + request_permission 接 UI + 流式渲染
- 阶段 09(hooks):pre/post hook 回调参数已就位(ToolUseQueue 构造)
- 阶段 10(compact):在 messages 增长处插入压缩
- 阶段 13(subagents):Task 工具直接调 AgentLoop(进程内嵌套 = 通信)
