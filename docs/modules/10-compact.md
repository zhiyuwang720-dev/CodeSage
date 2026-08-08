# 阶段 10 — compact 上下文压缩增强理解文档

> 分支 `feat/10-compact`,规格见 `docs/specs/10-compact.md`。落地 CC-15 三段(错误分类扣留 / 显式轮次状态 / transition reason)与 compact(10) 全项:length 截断恢复、manual /compact、熔断闭包化、boundary 固化。

## 模块职责

阶段 10 不改架构骨架,在既有循环上补齐「压缩什么时候该发生、发生了怎么恢复、失败了怎么兜底」:

| 文件 | 职责 |
|---|---|
| `engine/errors.py`(**新增**) | 错误分类器:RecoveryClass 枚举 + classify_recoverable(PTL 优先 → length → None) |
| `engine/loop.py` | 恢复阶梯接线(recovery_attempts 闸)、length 恢复、compact_now()、transition 8 写位 + --verbose 日志、熔断闭包 |
| `ai/types.py` + `ai/client.py` | `dropped_tool_uses` 信号(PI-03:length 截断剥除 tool_use 时计数) |
| `core/messages.py` | SessionMessage 瞬态字段 `dropped_tool_uses`(不进 to_dict,会话格式稳定) |
| `cli/commands.py` + `cli/repl.py` | `/compact` 命令注册 + 分发(isawaitable 支持 async handler) |

## 关键设计决策

### 1. 错误分类器:三分表,PTL 优先(CC-15 第一段)

`classify_recoverable(exc, stop_reason, ...)` 输出三类,只决定「是否走恢复」,不决定具体动作:

- **CONTEXT_OVERFLOW**:isinstance LLMError + is_ptl_error(400 PTL 文本 / 413)
- **OUTPUT_OVERFLOW**:`stop_reason == "length"`(截断是**成功响应**的属性,不是异常——R5)
- **None**:429/5xx 等,不恢复,落原错误路径

PTL 检查排在 stop_reason 检查之前(异常优先于响应属性);`last_block_is_truncated_tool_use` 参数保留但分类不再消费(S3 改由 dropped_tool_uses 信号驱动,不重复推断)。

### 2. 恢复阶梯 + 防死循环闸(CC-15 第二段 + §4)

`RunState.recovery_attempts: dict[RecoveryClass, int]` 统一记账:**每错误类每 turn 至多 1 次恢复动作**(值 0→1 后不再触发)。PTL 反应式压缩(§3.8)与 length 重发共用这一个闸——消除 S4 前 ptl_retried / length 各自记数的割裂。防死循环:恢复失败 → 落原路径/正常循环,绝不恢复-再失败-再恢复打转。恢复重试都计新 turn(while 顶 turn += 1,PTL 与 length 同约定;thinking recovery 的 turn -= 1 是唯一例外)。

### 3. 输出端 length 恢复:typed 信号跨三层(PI-03 + §3.2)

规格假设 loop 能看到「尾随残缺 tool_use」,但 client(PI-03)已在收集时剥除并置 is_error —— 若 loop 自己推断会双重实现。解法:`dropped_tool_uses: int = 0` typed 信号,LLMResponse → SessionMessage(瞬态,不落盘)→ loop,三层贯通:

- **形态 1**(length + 残缺 tool_use):剥除后 is_error + dropped_tool_uses>0 → loop 注入 OUTPUT_OVERFLOW_RECOVERY 轻量反馈(「残缺工具调用已丢弃,请重新发出」),恢复一次后闸置 1
- **形态 2**(纯文本截断):不恢复,截断文本照常 yield 落盘,下轮模型自愈
- **闸尽**:形态 1 的第二次 → 不恢复,按普通截断回复处理(rebuild 去掉 is_error),**不终止本轮**
- **LOW-3 守卫**:全空内容(纯 tool_use 被剥仅剩空文本)→ 重建无意义且空消息会落盘,按原 error 语义终止,不 yield

### 4. 熔断闭包化:config 只读,闭包挡 auto 不挡 manual(§7)

S5 前熔断 = 写 `config.compaction.enabled = False`(R4:与配置共享引用耦合)。改为实例闭包 `self._compaction_breaker: bool`:

- 触发:连续 2 次 generate_summary 失败(`_compact_failures >= 2`);**只挡 auto 触发点**(turn 顶部检查点 + PTL 反应式),config 字段永不写入
- 复位:压缩成功即 `_compaction_breaker = False`(唯一复位通道,auto/PTL 都走同一成功路径)
- manual 恒可用(§7.1 硬阻塞语义):闭包检查不在 `_compact` 内部,compact_now 天然绕过;manual 成功同样复位
- PreCompact 钩子失败(fail-open)不计入 `_compact_failures`——熔断只由摘要 LLMError 驱动

### 5. manual /compact 命令空间(§6)

- `loop.compact_now() -> bool`:调 `_compact(self._active_messages, trigger="manual")`;绕过防抖(不写 `_last_compact_turn`)与熔断;无可压内容(`_active_messages` 空 / `compaction is None`)/失败 → False;成功刷新 `_active_messages` 投影(meter 与下次 manual 都读它)
- REPL:`/compact` 注册于 COMMANDS,handler 是 **async**(注册表第一个)——分发点 `_handle_slash_command` 加 `inspect.isawaitable` 通用支持,既有 sync handler 零改动;成功打印「上下文已压缩」/失败「无可压缩内容」,**不清屏**(§6.4 裁剪:Kode 的清屏归阶段 12 UX)
- 单发模式(--print)不做命令分发
- PreCompact 钩子 trigger="manual" 走既有 matcher;exit-2 block 对 manual 同样尊重(用户配了 block 就是用户意图)

### 6. transition reason:词表 8 写位 + --verbose 单行日志(§5)

`_mark_transition(state, reason)` 统一写位 + `logger.info("transition: <值>")`(INFO 默认关,--verbose 开;不持久化,会话 append-only 不污染)。词表:

| 值 | 写位 |
|---|---|
| user_input | run() 入口接收本轮输入 |
| auto_compact | 阈值检查点 should_compact 命中 |
| ptl_compact | PTL 反应式压缩 |
| manual_compact | compact_now(**run 外无 state → 写实例投影**) |
| output_overflow | 形态 1 截断重发 |
| output_overflow_truncated | 形态 2/闸尽落回 |
| tool_result | 工具批量执行返回后 |
| error_terminate | 外 except LLMError(不可恢复);**裁决**:全空截断终止也写它(终止 ≠ 截断落回) |

run 内写位在 `state.last_transition`(RunState,每 run 独立),run() finally 投影到实例 `self.last_transition`(与 last_stop_reason 同模式);run 外写位(manual)直接写实例。单读面供调试/后续 boundary 使用。

### 7. boundary 消息模式:压缩摘要 = 唯一载体(§8,转述)

- 摘要消息(`is_compaction_summary=True`)是压缩点的**唯一边界载体**:不额外插空消息/说明消息;Kode 的 "Context automatically compressed…" 前置文案并入摘要内容开头(08 摘要 prompt 已含压缩原因提示)
- normalize 规则 5(`core/normalize.py:15,75-87` summary_idx)永不合并摘要,前后消息各自保位,会话 JSONL 结构稳定
- 摘要 = 「压缩前历史」与「压缩后继续」的硬分界:切点前消息不再进请求(find_cut_point 语义),摘要作为新起点
- 摘要自身可再次被后续压缩(链式,UPDATE 迭代处理「压缩后再压缩」)
- 固化:压缩后会话**含且仅含一条** is_compaction_summary;normalize 后摘要独立成条、内容不并入邻接 user

## 风险边界(规格落地情况)

- **熔断触发源无关**(LOW-1 确认):manual 失败共享 _compact_failures 计数是管线健康信号,与触发源无关,不处理
- **非 PTL provider error 不进 LLMError 路径**:error 流事件被 client 收集为 is_error 响应(loop.py:674 只对 PTL 文本 raise),error_terminate 实际触发面 = PTL 且恢复不可用 / 全空截断终止——测试按此形态构造
- **窗口可配不变**:CompactionConfig(window=...),manual 无配置(compaction=None)→ compact_now 返回 False
- **AGENTS.md 不参与权限**(设计不变量 #3 不变)

## 测试

- `tests/engine/test_errors.py`(12,新增):分类器三分表 + 异常优先于 stop_reason
- `tests/engine/test_loop.py`(+~25):length 恢复(形态 1 一次/形态 2/闸尽落回/LOW-3 空内容)、recovery_attempts 闸、熔断闭包(触发/复位/manual 旁路)、compact_now(成功/无可压/防抖/熔断)、transition 写位日志与投影、boundary 唯一载体 + normalize 保位
- `tests/engine/test_compact_events.py`(+2):PreCompact/PostCompact trigger="manual"、钩子失败不误触熔断
- `tests/cli/test_commands.py`(+3):/compact 注册/解析、分发成功与无可压两路径、HELP_TEXT 生成
- 全量:**825 passed, 9 skipped**(集成无 key skip;mtime flake 遇红重跑,非回归)
