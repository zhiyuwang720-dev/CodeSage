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

## 生产级强化(2026-08-05)

三轮修复(对照 Kode 审查,测试 170 → 337):

**修复内容**(批次 2 engine + 批次 3 E1):
- [高] thinking-only 重试:检测「无 tool_use 且无文本产出」,注入恢复消息,3 次有界 —— 不再静默终止
- [中] 超大工具结果落盘(engine 统一处理,不再撑爆消息)
- [中] 工具级消息流契约(逐工具 yield,不再整批合并)
- [中] max_turns 数值校验(非法值直接拒绝而非诡异行为)
- [低] abort 时未启动的兄弟工具跳过执行(E1)—— 中断语义更干净

**文件级判定**:
- A 类(已实现):E1(abort 兄弟跳过)落地
- B 类(映射阶段 X):压缩(10)、任务/todo(11)、会话生命周期(12)、子代理(13)、技能/斜杠(14)、MCP(15)、记忆(17)
- C 类(理由):daemon 域(agentEvents/stream-json 协议)、checkpoints

**现状**:及格偏下 → 良好。max_budget 死代码与 thinking-only 静默终止消除,消息流契约完整;最大剩余项是微/自动压缩(O(n²) 归一化),排期阶段 10。

## 设计决策剖析

### 为什么这么设计

1. **显式 while 迭代替代递归 generator** —— Kode 的 messagePipelineCore 递归 `yield*` 自身,Python 在 ~1000 轮(默认 recursionlimit)RecursionError —— 计划里的 R1 风险。while + 可增长消息列表:同一透明消息流、零栈增长(2001 轮压力测试通过,16 秒)。
2. **四类终止条件,消息即产物** —— 终答 / max_turns / max_budget / abort 全部以消息形式 yield(is_meta 说明消息),循环本体不抛异常结束。动机:消费方(CLI/测试)对终止类别无感知,统一走消息流。
3. **错误自愈:工具失败转 is_error tool_result** —— 抛异常/权限拒绝/未知工具/非法输入全部转 error 结果进消息列表,模型自己调整;唯一硬性停止是 max_turns/max_budget。动机:harness 对工具失败的容错不靠重试,靠"让模型看见失败"。
4. **ToolUseQueue:并发屏障 + sibling 作废** —— 安全工具 gather 并行,非安全工具独占一批;批内失败 → 同批 sibling 与后续队列全部作废为 error。动机:并行工具调用有隐式依赖(先写后读),失败前提下的继续执行会喂幻觉。
5. **abort 协作式中断 + thinking-only 有界重试** —— asyncio.Event 三检查点;只输出 thinking 无产出时注入恢复消息,3 次有界。动机:中断与空转都不能破坏持久化不变量,也不能无限烧钱。

### 设计原则

- **单一信息通道**:Message 流是唯一通道,工具结果、中断、错误都是消息
- **错误自愈**:循环永不因工具失败而崩,唯一硬性停止是有界预算
- **安全默认**:ask 无回调 → 拒绝,绝不默认放行
- **有界性**:max_turns / max_budget / 重试次数 / 结果落盘阈值全部有界
- **可测可注**:依赖全部构造注入,session 可选可关,queue 结果可断言

### 优点

- 零栈增长:2001 轮压力测试通过(测试设计为"超过默认限制 2 倍"),递归版 1000 轮必炸
- 工具级消息流契约:逐工具 yield tool_result,阶段 07 UI 可边收边渲染
- sibling 作废显式建模"并行调用间的依赖风险",模型被迫处理失败而非假装成功
- 超大结果(>100K 字符)落盘 + 500 字符指针,不撑爆上下文
- abort 语义干净:一条消息、一个 Event、兄弟工具跳过执行(E1)

### 为什么不选用别的技术方案

| 备选方案 | 为什么不选 |
|---|---|
| 递归 async generator(Kode 做法) | Python recursionlimit 1000,~1000 轮 RecursionError(R1 风险);while 迭代零栈且显式持有检查点与计数 |
| 状态机库/图(transitions 等) | 循环 + 三检查点 + 终止条件已完整表达;状态机引入抽象成本,异步流式场景无收益 |
| signal/KeyboardInterrupt 做中断 | asyncio.Event 在事件循环内协作式检查、跨平台一致;signal 在线程上下文复杂、Windows 行为不同,且无法在工具内共享检查 |
| 每轮缓存 tools.specs() | 7 个内置对象重建便宜;MCP(阶段 15)数量上来再缓存,源码 ponytail 注释明示 |
| 流式事件架构(Kode agentEvents) | collect() 拼装语义完整、实现简单;07 渲染升级时接口已支持流式(逐工具 yield) |

## 面试问题整理

### 技术点清单

显式 while 迭代(零栈)/ 四类终止 + is_meta 消息 / 错误自愈(失败转 error tool_result)/ ToolUseQueue 并发屏障与 sibling 作废 / abort 协作式中断 / thinking-only 有界重试与超大结果落盘

### 面试问题与答案

**Q: 为什么主循环不用递归 generator?**
**A: Kode 的 messagePipelineCore 递归 `yield*` 自身;Python 复制会在 ~1000 轮(默认 recursionlimit)RecursionError —— 计划里的 R1 风险。CodeSage 用显式 while + 可增长消息列表:同一透明消息流、零栈增长,2001 轮压力测试(超过默认限制 2 倍)通过,16 秒。**
**深度衍生: while 版与递归版的消息流语义有区别吗?** → **没有:两者都是"assistant → tool_results → assistant"序列,yield 即消费,消费方无感知。差别在内部:while 版同栈帧推进,天然持有 abort 检查点与轮次计数;已知代价是每轮 normalize 增长消息列表 O(n²),阶段 10 压缩解决真实场景。**
**广度衍生: asyncio 递归为何更危险?** → **协程仍运行在同一 Python 栈上,递归 await 一样吃栈帧,且异步递归错误常在 collect/超时回调里才暴露,更难定位。Python 3.11+ 的协程栈追踪能缓解排查,但长生命周期循环用迭代是语言惯用法 —— 与 Node 事件循环同理。**

**Q: 工具失败后会发生什么?**
**A: 工具抛异常、权限拒绝、未知工具、非法输入 → 全部转成 is_error 的 tool_result 消息进消息列表,模型看到失败自己调整 —— 循环永不因工具失败崩溃。唯一硬性停止是 max_turns/max_budget。未知工具(_MissingTool)与非法输入(validate_input 抛 ToolError)不排队执行,直接带 error 结果标记 completed,不污染兄弟。**
**深度衍生: 为什么"一个工具失败 → 同批 sibling 全部作废"?** → **同批工具由模型并行发起,失败意味着前提可能不再成立(先写后读),sibling 继续执行会产出"基于失败前提的结果",模型可能当真相。统一收 `<tool_use_error>Sibling tool call errored</tool_use_error>`,模型必须显式处理;作废成本低,错误前提下的继续执行成本高。**
**广度衍生: 与 Anthropic 官方 agent 循环的错误处理差异?** → **一致处:错误都放 tool_result 让模型自愈。差异在粒度:官方样例只做"该工具自己的结果",CodeSage 额外做了批级作废与队列级作废 —— 把"并行调用有隐式依赖"显式建模,不依赖模型自己避错。**

**Q: 一批工具调用怎么调度?非安全工具为什么是屏障?**
**A: ToolUseQueue 按 is_concurrency_safe 分批:连续安全工具 gather 并行,遇非安全工具(Bash)即屏障独占一批,防并发写竞争。批内任一失败:同批其余 sibling 与后续所有队列项作废为 error;执行前检查 abort(E1),已中断的兄弟工具直接返回 "(interrupted by user)" 不启动。**
**深度衍生: 为什么不做运行时依赖分析?** → **信工具自声明(is_concurrency_safe)而非分析输入:依赖分析要么保守(全串行,丢并行收益)要么不安全(漏分析)。契约驱动让引擎保持简单,工具作者为并发安全负责 —— 与 needs_permissions 自声明同一哲学。**
**广度衍生: gather 的 return_exceptions=True 为何必要?** → **把每个任务的异常装进结果列表,不打断其它任务的并发执行,zip(batch, results) 能把异常归属到具体工具;直接 await 会在第一个异常处中断并丢失其余结果 —— 而判断 any_error 需要先收齐全部结果,作废语义依赖它。**

**Q: 中断(abort)怎么实现?为什么用 asyncio.Event?**
**A: AgentLoop 持有一个 asyncio.Event,检查点三处:循环顶部、LLM 调用后(collect 返回)、工具批执行前;工具通过 ToolUseContext.abort_event 同查。中断产物是 "(interrupted by user)" 的 is_meta 消息,normalize_for_api 过滤,不进模型上下文。CLI 的 SIGINT/SIGTERM handler 只 set 事件,第二次按下直接 exit(130)。**
**深度衍生: 为什么不做流式调用的即时取消?** → **流式收包中取消语义复杂(部分 token 已产出、供应商连接状态不可知)。协作式检查点代价是"最多延迟一轮",收益是取消路径只有一条:is_set → yield is_meta → return,持久化不变量(JSONL 不写半条)永远成立。一个 Event 多个消费点,这是 signal handler 做不到的。**
**广度衍生: 对比传统 SIGINT 默认杀进程,协作式中断的取舍?** → **传统做法在信号处立即终止;协作式把"杀"延迟到安全点 —— 对 harness,安全点是"一条消息完整产出后",保证会话与审计不写半条。代价是当前轮可能浪费,收益是数据一致性由消息边界决定而非信号时机 —— 与数据库 checkpoint 同理。**

**Q: thinking-only 回复为什么要重试?怎么保证有界?**
**A: 模型只输出 thinking 块、无文本无 tool_use 时直接返回就是静默终止。_is_thinking_only 检测后注入恢复消息("你只输出了内部思考…"),turn 回退不计轮次,3 次(THINKING_ONLY_MAX_RETRIES)后放弃并产 is_meta 消息 —— 有界保证不无限空转烧钱。**
**深度衍生: turn -= 1 的语义是什么?** → **恢复消息是 harness 注入的 nudge 而非模型产出,不应消耗用户轮次预算;thinking_retries 单独计数,与 turn 解耦 —— 重试预算与轮次预算是两个独立有界维度,分别防空转与防过长。**
**广度衍生: 超大工具结果落盘为什么也放引擎?** → **_spill_large_result 在 queue 收结果后统一处理:>100K 字符写临时文件,模型只见 "(result saved to ...)" + 500 字符预览。放引擎而非工具层,保证所有工具(含第三方)共享同一上限,工具作者无需自管 —— 与权限决策链同理,横切关注点在引擎单点。**
