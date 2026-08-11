# 阶段 10:compact 上下文压缩增强

> 基于:阶段 08 压缩系统(已实现)+ 阶段 09 Hook 系统(已实现)+ todo.md CC-15 / compact(10) 需求 + Claude Code / Kode-CLI 实现探索(worker-3)。
> 前置规格:`docs/specs/08-context.md`、`docs/specs/09-hooks.md`、`docs/specs/codesage.md`。

## 0. 验收标准(tasks/todo.md CC-15 + compact(10) 条目)

- [ ] `/compact` 手动命令注册并可用(repl 注册点 `cli/commands.py:50` COMMANDS 列表,repl.py:344 find_command 分发;loop 公开 `compact_now()`)
- [ ] manual 触发走 `trigger="manual"`,`PreCompact` 钩子 matcher 按 trigger 精确匹配(`hooks/registry.py:61-68` 已支持,无需改)
- [ ] 熔断(`compaction.enabled=False`)只挡 auto 触发,**manual 永远可用**(硬阻塞语义成文,§7)
- [ ] 熔断复位路径:压缩成功即清零(既有 `_compact_failures = 0`,`loop.py:496`),manual 成功同样复位
- [ ] 错误分类器把 413/PTL(已有)与 max_output_tokens / stop_reason=="length"(新增)统一归入「可恢复」,其余错误走原终止路径
- [ ] `stop_reason == "length"` 且末消息含残缺 tool_use → 恢复动作(截断/提示继续),不再静默丢弃
- [ ] 恢复阶梯:每错误类每 turn 至多一次恢复动作(防死循环闸),超限走原 error 路径
- [ ] RunState 记录 transition reason 与恢复尝试(`recovery_attempts`),显式轮次状态对象语义成文(§5)
- [ ] boundary 消息模式定义成文(§8):摘要消息为唯一载体,normalize 永不合并(已实现,测试固化)
- [ ] 全量回归:`python -m pytest tests/ -q` 全绿(当前基线 797 项)

## 1. 目标与范围

### 1.1 做什么

阶段 10 补齐 08 压缩的**用户出口与失败恢复**两个方向,核心来自 todo.md 两条:

| 需求 | 原文(todo.md) | 落位 |
|---|---|---|
| CC-15 错误恢复 | 「可恢复错误先扣留(413/max_output_tokens)→ 恢复阶梯(compact→升级重试)→ 防死循环闸;显式轮次状态对象 + transition reason(现任何 LLMError 直接终止,零恢复)」 | §2-§5 |
| compact(10) | 「熔断器已入 08(§3.5);10 保留:硬阻塞预留手动 /compact 空间 + boundary 消息模式」 | §6-§8 |

具体交付:

1. **错误分类/扣留层**(§2):把 08 的 `is_ptl_error`(413/400 PTL)从「专线」提升为「分类器」,新增输出端溢出(max_output_tokens / stop_reason=="length")归类。可恢复错误先扣留(不立即终止),进入恢复阶梯。
2. **输出端(length)恢复**(§3):`stop_reason == "length"` 是成功响应不进 LLMError 路径(R5),目前残缺 tool_use 被静默丢弃——补独立分支。
3. **恢复阶梯 + 防死循环闸**(§4):compact → 升级重试 → 防死循环闸。每错误类每 turn 一次,超限走原错误路径。
4. **显式轮次状态对象 + transition reason**(§5):RunState 扩展,错误恢复不再是散落 if。
5. **manual `/compact` 命令空间**(§6):repl 注册 + `loop.compact_now()` 公开,复用 08 摘要管线,PreCompact/PostCompact 钩子随 trigger 匹配。
6. **熔断复位与硬阻塞语义**(§7):熔断只挡 auto;manual 永远可用;压缩成功复位。
7. **boundary 消息模式成文**(§8):08 已实现(`is_compaction_summary` + normalize 保位),本阶段补定义 + 测试固化。

### 1.2 不做什么(与 CC/Kode 的裁剪)

| 候选(探索发现) | 来源 | 裁决 |
|---|---|---|
| 全量压缩不留 keep_recent | Kode autoCompactCore.ts | ❌ 08 切点语义 + split-turn 前缀摘要信息保留更精细 |
| Session Memory 压缩变体 | 官方私有 flag(tengu_sm_compact) | ❌ 依赖私有 feature,价值低 |
| Task/Skill/MCP 快照注入摘要 | Kode compactionSnapshots.ts | ❌ CodeSage 无官方式 task list;PreCompact instructions 通道已覆盖 |
| 分级 TokenWarning UI | CC TokenWarning.tsx | ❌ statusbar ctx meter 已有,UX 交互归阶段 12 |
| Esc 跳过 auto-compact | 官方 pre-compact skip | ❌ PreCompact 钩子 exit-2 block(09 §7.4)已等价 |
| 固定 margin 阈值(limit-13K) | Kode autoCompactThreshold.ts | ❌ 08 window/reserve 已可配,DeepSeek 128K 下 111,616(87%)合理;换模型时再评 |
| SessionSummaryRecord(resume 稳定摘要) | Kode autoCompactCore.ts:347-366 | ❌ 会话生命周期归阶段 12 |
| compact 模型 fit 检查回退 main | Kode autoCompactCore.ts:251-280 | ❌ 128K 场景 compact/main 同窗口概率高;升级重试降级形态(§4.2)已覆盖意图 |

### 1.3 三分法边界

- **08 已有,10 复用**:触发检查点(turn-top,loop.py:257-286)、PTL 反应式(loop.py:314-335)、`_compact` 管线(loop.py:452-506)、切点语义(find_cut_point,compaction.py:138)、摘要生成(generate_summary,compaction.py:300)、UPDATE 迭代、防抖(`_last_compact_turn`)、熔断计数(`_compact_failures`)、PreCompact/PostCompact 钩子(loop.py:970-1037)、boundary 消息(is_compaction_summary)。
- **10 新增**:错误分类器、length 输出端恢复、恢复阶梯闸门、RunState transition reason、`/compact` 命令、熔断复位语义。
- **08 语义微调**(红线,需回归):熔断从「写 `compaction.enabled=False` 共享引用」(R4)改为实例级闭包(§7.2);PTL 路径恢复动作从专线 `ptl_retried` 并入统一 `recovery_attempts`(§4.3)。

## 2. 错误分类与扣留层(CC-15 第一段)

### 2.1 现状

- `is_ptl_error`(`ai/retry.py:41-51`):HTTP 413 或 400 含 `context_length_exceeded`/`prompt_too_long` → PTL 反应式压缩(loop.py:314-335)。
- 429/5xx:`client.complete` 自管重试,尊重 retry-after(`ai/client.py:121-134`)。
- **其余任何 LLMError → run() 外层 error 路径,本轮终止**——即 todo 原文「现任何 LLMError 直接终止,零恢复」。
- `stop_reason == "length"` 是成功响应,不进 LLMError 路径;残缺 tool_use 仅被丢弃,无恢复(R5)。

### 2.2 新增:统一分类器

`engine/errors.py` **新增**(或 loop.py 内私有,实施时定——倾向独立小模块便于测试):

```python
class RecoveryClass(Enum):
    CONTEXT_OVERFLOW = "context_overflow"   # 413 / PTL 文本(已有路径,并入统一闸门)
    OUTPUT_OVERFLOW  = "output_overflow"    # max_output_tokens / stop_reason=="length"(新增)

def classify_recoverable(exc: BaseException | None,
                         stop_reason: str | None,
                         last_block_is_truncated_tool_use: bool) -> RecoveryClass | None:
    """None = 不可恢复,走原错误路径。每类错误只在恢复闸门允许时执行一次恢复动作。"""
```

判定规则:

| 输入 | 类别 |
|---|---|
| `is_ptl_error(exc)`(413 / 400 PTL) | `CONTEXT_OVERFLOW` |
| `stop_reason == "length"`(或响应带 `max_output_tokens` 语义) | `OUTPUT_OVERFLOW` |
| 其他 | `None`(原路径) |

扣留语义:「先扣留」= 分类为可恢复后不立即终止,交给 §4 恢复阶梯裁决;阶梯判超限才落回原错误路径。

## 3. 输出端(length)恢复

### 3.1 问题

`stop_reason == "length"` 出现于两种形态:

1. **残缺 tool_use**:assistant 末 block 是 tool_use 且无配对 tool_result(输出被截断,工具调用不完整)——目前静默丢弃,模型下轮可能重复调用或陷入混乱。
2. **纯文本截断**:回复被截断但无工具调用——可接受(下轮模型会继续),低优先级。

### 3.2 设计

- 检测点:`_ask_model` 返回后(loop.py:508-568),`response.stop_reason == "length"` 且末消息为 assistant 且末 block 是未配对的 tool_use。
- 恢复动作(仅形态 1):**残缺回复整体不进会话**(实施修正:PI-03 已在 client 层剥除残缺 tool_use 并置 `dropped_tool_uses` 信号,loop 层经该信号判定形态 1,loop.py:350-359),向模型发一条轻量反馈(「请直接重新发出该工具调用」),记 `recovery_attempts[OUTPUT_OVERFLOW] += 1`,重试一次。
- 仍失败或已是恢复后重试 → 计入防死循环闸,落回正常循环(下轮模型自愈),**不终止本轮**。
- 形态 2(纯文本截断)不恢复,仅记 transition reason(§5)。
- **例外(实施修正,loop.py:363-367)**:闸尽且回复全空(纯 tool_use 被 PI-03 剥除、无任何非空 text 块)→ 重建无意义且空消息会污染会话,回原 error 语义终止(`last_stop_reason="error"`,不 yield 不落盘)。

## 4. 恢复阶梯(compact → 升级重试 → 防死循环闸)

### 4.1 阶梯定义(CC-15 原文展开)

| 级 | 动作 | 适用 |
|---|---|---|
| 0 | 无恢复,走原 error 路径 | 不可恢复错误 |
| 1 | **compact**(复用 `_compact`,trigger 沿用实际来源) | `CONTEXT_OVERFLOW`(PTL 路径已有,并入闸门) |
| 2 | **升级重试** | 压缩后仍失败 |
| 3 | **防死循环闸**:每错误类每 turn 至多一次恢复动作 | 全部 |

### 4.2 升级重试的落地形态(裁决 R6)

探索确认:模型层无第二模型指针支撑「升级」(R6);compact 模型 fit 检查亦被裁剪(§1.2)。落地形态 = **「压缩 → 用 main 指针重试一次」**,即压缩已腾出窗口,重试走主循环既有路径(非新通道)。`CONTEXT_OVERFLOW` 的压缩后重试即 08 既有 PTL 行为(loop.py:328 后 retry),本阶段只把它从专线并入统一闸门;`OUTPUT_OVERFLOW` 无压缩诉求(上下文未爆),恢复动作即 §3.2 截断重发。

### 4.3 防死循环闸(RunState 统一记账)

08 现状:`ptl_retried: bool`(RunState,loop.py:136)专管 PTL「每 turn 一次」。

10 改为:**`recovery_attempts: dict[RecoveryClass, int]`**(RunState,per-run,loop.py:124-139 扩展)。规则:

- 每个错误类每 turn 至多 1 次恢复动作(值 0→1 后不再触发);
- 恢复动作**成功** → 清零(下 turn 新对象自然清零);
- 恢复动作**失败**(如压缩失败)→ 落回原错误路径(08 已有语义:PTL 压缩失败计入熔断,loop.py:492-495),不再循环(「没有 PTL-compact-PTL 循环」注释,loop.py:326 已是此意,统一后自然保持);
- PTL 反应式路径从 `state.ptl_retried` 迁移到 `state.recovery_attempts[CONTEXT_OVERFLOW]`(保持字段为兼容投影或直接替换,实施时定;行为红线:每 turn 至多一次)。

## 5. 显式轮次状态对象 + transition reason(CC-15 第三段)

### 5.1 现状

`RunState`(`engine/loop.py:124-139`)已是显式 per-run 对象(阶段 06/09 重构产物):`messages / turn / thinking_retries / ptl_retried / last_cache_read / stop_feedback_count / permission_denials`。CC-15 的「显式轮次状态对象」已大体落位,本阶段补 **transition reason**。

### 5.2 新增字段

```python
last_transition: str | None = None      # 最近一次状态迁移原因(§5.3 词表)
recovery_attempts: dict[RecoveryClass, int] = field(default_factory=dict)  # §4.3 闸门数据源
```

### 5.3 transition reason 词表(写事件点)

| 值 | 写位 |
|---|---|
| `"user_input"` | run() 入口接收本轮输入 |
| `"ptl_compact"` | PTL 反应式压缩触发(loop.py:327 前) |
| `"auto_compact"` | 阈值检查点压缩(loop.py:281 前) |
| `"manual_compact"` | `/compact`(§6) |
| `"output_overflow"` | §3.2 截断重发 |
| `"output_overflow_truncated"` | §3.1 形态 2(纯文本截断,不恢复) |
| `"tool_result"` | 工具执行返回(执行后恢复点) |
| `"error_terminate"` | 不可恢复错误落原路径 |

用途:审计/调试单行可见「这轮为什么这样走」;`--verbose` 日志打一行即可,不做持久化(会话 append-only 不污染)。

## 6. manual `/compact` 命令空间

### 6.1 命令注册

- `cli/commands.py:50` COMMANDS 列表新增 `/compact` 条目(description:「压缩上下文」),`find_command`(commands.py:58)分发无需改(repl.py:344 已有通用分发)。
- 09-hooks.md:690 预留的「repl 注册 + loop 公开 compact_now()」落位。

### 6.2 `loop.compact_now()`(loop.py 新增公开方法)

```python
async def compact_now(self) -> bool:
    """manual 触发压缩。绕过防抖与熔断(§7);返回是否压缩成功。"""
    compacted = await self._compact(self._active_messages, trigger="manual")
    ...
```

- `trigger="manual"`(loop.py:456 已预留,v1 恒 "auto")。
- **防抖**:manual 不设置 `_last_compact_turn`(08 防抖仅 auto 检查点写,loop.py:278;manual 天然不受限)。
- **熔断**:`enabled=False` 不挡 manual(§7.1 硬阻塞语义)。
- **无消息可压**(如空会话)→ `_compact` 返回 None → 返回 False,repl 提示「无可压缩内容」。
- 成功后 `_active_messages` 投影刷新(loop.py:286 同款)。

### 6.3 REPL 侧

- 交互输入 `/compact` → 调 `loop.compact_now()`(非任务,直接 await);完成后打印一行结果(boundary 消息由摘要管线落盘,REPL 不额外渲染历史)。
- 单发模式(--print)不做命令分发,不涉及。
- PreCompact 钩子 trigger="manual" 匹配(registry.py:61-68 matcher 按 trigger 已支持,09 §2.2);exit-2 block → manual 也尊重(用户配了 block 就是用户意图)。

### 6.4 清屏/可见性(裁剪决定)

Kode 手动 /compact 清屏(compact.ts:164-166,191)。10 不做清屏(REPL 交互归阶段 12 UX),仅打印结果行。

## 7. 熔断复位与硬阻塞语义

### 7.1 硬阻塞语义(成文,裁决 todo 原文)

todo「硬阻塞预留手动 /compact 空间」解读:**熔断是 auto 路径的硬闸门(挡死自动压缩),但它必须给 manual 留出通道**——熔断是「系统不再自动烧摘要钱」的自我保护,不是「用户不再能压缩」。故:

- `compaction.enabled == False`(熔断)→ 挡 auto 检查点(loop.py:265)与 PTL 路径(loop.py:319);
- manual `/compact` **恒可执行**(显式用户意图,不设防);
- 文档与 help 文本写明此语义。

### 7.2 复位路径 + 共享引用解耦(R1/R4)

现状:`_compact_failures` 达 2 → `self.compaction.enabled = False`(loop.py:492-495)直接 mutate `AgentLoopConfig` 的共享引用(R4);无任何复位路径(R1)。

10 改:**熔断状态收归实例级闭包,config 只读**:

```python
# loop.py 实例字段(替代直接写 config.enabled)
self._compaction_breaker: bool = False   # True = 熔断中(仅挡 auto)
```

- 触发:连续 2 次 `generate_summary` 失败(语义不变,loop.py:492-495 改写闭包);
- 复位:压缩成功(`_compact_failures = 0` 处,loop.py:496 同点)→ 闭包清零;
- manual 成功同样复位(成功即证明摘要管线健康);
- 所有「enabled」判断点(loop.py:265/319 及新 manual 入口)统一读 `compaction.enabled and not self._compaction_breaker`,manual 只读闭包跳过。
- 遗留:`compaction.enabled` 字段保留(配置可见性/测试断言),不再被运行时写。

### 7.3 测试注意

既有红线测试可能断言 `compaction.enabled` 熔断写(08 契约)→ 断言点迁移到闭包语义(§9.2 红线表更新)。

## 8. boundary 消息模式(成文)

### 8.1 定义

**boundary 消息 = 压缩摘要消息本身**,携带 `is_compaction_summary: bool = True`(08 已实现,loop.py 摘要落盘 + `core/normalize.py:15,75-87` 保位不合并)。语义:

1. 摘要消息是压缩点的**唯一边界载体**,不额外插入空消息或说明消息;
2. normalize 永不合并它(前后消息保位,会话 JSONL 结构稳定);
3. 它是「压缩前历史」与「压缩后继续」的硬分界:切点之前的消息不再进请求(08 `find_cut_point` 语义),摘要作为新起点;
4. 摘要消息自身可再次被后续压缩(链式,UPDATE 迭代已在 compaction.py:213-234 处理「压缩后再压缩」)。

### 8.2 本阶段补充

- 上述定义写进本文档(§8.1),并在 `docs/modules/10-compact.md`(实施后写)转述;
- 测试固化:新增断言「压缩后会话含且仅含一条 is_compaction_summary 消息,normalize 后边界保位」(§9.1)。
- Kode 的 "Context automatically compressed due to token limit…" 前置说明文案:并入摘要消息内容开头(08 摘要 prompt 已含压缩原因提示,不加新消息——保持「唯一载体」)。

## 9. 测试计划

### 9.1 镜像清单(`tests/…`,镜像实现文件)

| 测试文件 | 镜像 | 用例要点 |
|---|---|---|
| `tests/engine/test_errors.py` **新增** | `engine/errors.py`(新增) | 分类器三分表:413→CONTEXT_OVERFLOW / 400 PTL→CONTEXT_OVERFLOW / length+残缺 tool_use→OUTPUT_OVERFLOW / length+纯文本→OUTPUT_OVERFLOW(不恢复)/ 429、5xx、其他→None |
| `tests/engine/test_loop.py` 增 | `engine/loop.py` | manual 直调 `compact_now()`(trigger 传参、防抖不设、熔断下可执行);length 截断重发(残缺 tool_use 恢复一次、纯文本不恢复);recovery_attempts 每 turn 一次;transition reason 各写位;熔断闭包(触发/复位/manual 旁路) |
| `tests/engine/test_compact_events.py` 增 | hooks 接线 | manual trigger 的 PreCompact/PostCompact 事件 trigger 字段 = "manual";matcher 按 trigger 过滤 |
| `tests/cli/test_commands.py` 增(如无此文件则并入 repl 测试) | `cli/commands.py` | `/compact` 注册于 COMMANDS 且 find_command 可解析;/help 显示 |
| `tests/engine/test_compaction.py` 增 | `compaction.py` | boundary 唯一载体断言(会话含且仅含一条 is_compaction_summary) |

### 9.2 不能破坏的既有契约(10 改动红线)

| 红线 | 锚点 | 说明 |
|---|---|---|
| 压缩防抖(每 turn 至多一次 auto) | loop.py:265/278 | manual 不设 `_last_compact_turn`,但 auto 语义不变 |
| 熔断(连续 2 次失败停 auto) | loop.py:492-495 | 语义不变,写点从 config 迁闭包(§7.2) |
| 切点语义:user 整轮/assistant 拆轮/tool_result 载体永不切 | compaction.py:111-168 | 不动 |
| boundary 保位:normalize 永不合并 is_compaction_summary | core/normalize.py:15,75-87 | 不动,加测试固化 |
| PTL 路径每 turn 至多一次压缩 | loop.py:314-335 | `ptl_retried` → `recovery_attempts` 迁移后行为等价(回归) |
| 摘要生成 UPDATE 迭代、失败 graceful | compaction.py:213-300 | 不动 |
| 会话 append-only:压缩恰追加一条摘要 | loop.py:503 | 不动 |
| PreCompact exit-2 block 语义(09 §7.4) | loop.py:1004 | manual 同样尊重 |

### 9.3 回归

`python -m pytest tests/ -q` 全绿(基线 797,新增用例后增长);含 08/09 全部压缩与钩子用例。

## 10. 实施步骤

| 步 | 内容 | 文件:锚点 | 闸门 |
|---|---|---|---|
| S1 | 错误分类器模块 + 单元测试 | `engine/errors.py` **新增**;retry.py:41-51 复用 | 新测试绿 |
| S2 | RunState 扩展:`last_transition` + `recovery_attempts`(`ptl_retried` 保留,迁移归 S4) | loop.py:124-139/136 | 全量回归绿 |
| S3 | 输出端恢复:length + 残缺 tool_use 检测与截断重发 | loop.py `_ask_model` 返回点(L508-568 区间) | S2 后,全绿 |
| S4 | 恢复阶梯统一闸门:PTL 专线并入 `recovery_attempts` | loop.py:314-335 | 全绿(PTL 行为回归) |
| S5 | 熔断闭包化 + 复位路径 | loop.py:492-496;enabled 读点 265/319 | 全绿(熔断回归) |
| S6 | `loop.compact_now()` 公开 + manual trigger 接线 | loop.py `_compact` 旁 | S5 后,全绿 |
| S7 | `/compact` 命令注册 + REPL 分发 | commands.py:50、repl.py:344 | S6 后,全绿 + 手动验证 |
| S8 | transition reason 各写位 + `--verbose` 单行日志 | loop.py 各写点 | 全绿 |
| S9 | boundary 语义测试固化;`docs/modules/10-compact.md` 理解文档 | 测试 + docs/modules/ | 全绿 |

依赖:S1 → S2 → S3;S2 → S4 → S5 → S6 → S7;S8 独立可并行;S9 收尾。步骤间每步独立提交。

依赖外部:02 ai(client.complete 重试语义)、03 tools、08 压缩全部、09 钩子(PreCompact trigger 匹配)——全部已就绪,零新依赖(Ask first 不触发)。

## 11. 风险与边界

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 熔断永久锁死(无复位路径,enabled=False 后 PTL 锁死至实例销毁) | §7.2 闭包 + 复位路径;manual 恒可用 |
| R2 | 防抖不覆盖 PTL 路径(同轮两次压缩可能) | 现状经 `state.ptl_retried` 已挡;迁移后 `recovery_attempts` 双保险(S4 回归) |
| R3 | `_compact` 返回 None 语义混淆(无可压/失败/阻止同为 None) | 本阶段不动返回值契约;文档注明三义,`compact_now` 对外只暴露 bool |
| R4 | 熔断经共享引用 mutate AgentLoopConfig(frozen 只挡直接赋值) | §7.2 闭包化,config 只读 |
| R5 | length 是成功响应不进 LLMError 路径 | §3 独立分支,不依赖分类器的异常通道 |
| R6 | 升级重试无模型层支撑 | §4.2 裁决:降级为「压缩 + main 重试」,不造新通道 |
| R7 | manual 与 auto 竞态(用户 /compact 撞上检查点压缩) | 同一 `_compact` 管线单飞(防抖 turn 级),manual 不设防抖但检查点在 turn 顶先跑,自然串行 |
| R8 | `ptl_retried` 迁移破坏既有测试断言 | §9.2 红线回归;保留字段投影过渡或一次性迁移 |

## 12. 与路线图的关系

- **CC-15 全量落位**(todo.md:92):扣留 → 阶梯 → 闸门 + transition reason,本阶段完成;阶段 12(会话生命周期)接 SessionSummaryRecord 与 UX 交互。
- **compact(10) 全量落位**(todo.md:98):manual 空间 + boundary 模式,本阶段完成。
- **阶段 12**:会话 UX(CC-16:标题/标签/粘贴引用化)、SessionSummaryRecord(resume 稳定摘要,探索 §1.2 裁剪项回炉)。
- **阶段 13(子代理)**:错误分类器与恢复闸门是子代理失败处理的复用基础。
- **阶段 17(记忆)**:与压缩的交互(摘要 vs 记忆提取)届时再评,不阻塞本阶段。

---

*附:探索依据(worker-3 报告,2026-08-08)Kode-CLI `packages/core/src/utils/autoCompactThreshold.ts`(固定 margin 13K)、`autoCompactCore.ts`(8 节模板 + 快照 + 全量压缩)、`compact.ts`(/compact 命令)、CC 官方(3 次断器、PreCompact skip、Session Memory 变体)。对照结论:08 的切点语义、UPDATE 迭代、PTL 反应式三项领先,本阶段不后退。*
