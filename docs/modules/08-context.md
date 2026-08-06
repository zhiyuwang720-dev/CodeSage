# 阶段 08 — 上下文工程理解文档

> 分支 `feat/08-context`,规格见 `docs/specs/08-context.md`。落地 CC-12/13/14 与 PI-05。

## 模块职责

让模型「记得住、看得懂、不超窗」的三件套,全部在引擎层(engine/)实现:

| 文件 | 职责 |
|---|---|
| `engine/context.py` | 会话级上下文:AGENTS.md 逐层收集/预算/override + git 快照 + 日期,一次组装(CC-13 memoize) |
| `engine/tokens.py` | usage 锚点估算 + should_compact 阈值(pi 同款,从不调 API 估上下文) |
| `engine/compaction.py` | 压缩三件套:find_cut_point / serialize / 摘要生成 + fileOps 恢复 + 旧结果清理 |
| `engine/loop.py` | turn 顶部压缩检查点 + reminder 注入 + 恢复/清理接线 |
| `core/messages.py` + `core/normalize.py` | is_reminder / is_compaction_summary 字段 + 前置合并 / 永不合并规则 |

## 关键设计决策

### 1. 三层裁剪:五级 → 三级

Claude Code 五级压缩(预算 → snip → micro → collapse → auto)裁成两级:**microcompact(旧结果占位)→ auto-compact**。理由见规格 §1:我们上下文规模小、模型窗口大(128K/200K),为服务端缓存设计的 L4 Context Collapse 收益不抵复杂度;L1 大结果落盘未做(工具层已截断,Bash 30K/Grep MAX_RESULTS,无跨轮恢复需求);L2 snip 为 feature-gated,合并进旧 tool_result 清理一步。**不做 cache_edits / API 端 context management**(Anthropic beta,DeepSeek 无对应)——只做「客户端不主动破坏前缀」的被动配合。

**顺序是硬约束,不是巧合**:两级在 turn 顶部检查点按 CC query.ts:379-468 的顺序执行——microcompact 先跑,阈值估算用**清理后的视图**;清理释放足够 token 时 auto-compact 根本不触发(LLM 摘要是最后手段)。摘要生成仍对 **RAW 消息**(非清理视图)进行,保真不损;阈值估算不做 freed 修正,与 CC 一致(CC 只对 snip 做 snipTokensFreed 修正,usage 锚点看不到清理节省,倾向更早压缩,方向安全)。

### 2. system-reminder 注入:静态 system + 前置 reminder

S4 把 context 从 system prompt 里拆出来(每轮重建 → 会话一次):
- `system` = base 提示词,字节级稳定 → Anthropic 可打 cache_control 断点、DeepSeek 自动前缀缓存命中
- context 渲染成**一条**前置 user 消息(REMINDER_HEADER + `# title` 分段 + FOOTER),上限 10 段;date/git 恒保留,AGENTS.md 超限丢**最远**段
- reminder 永不持久化(resume 时由 bundle 重建),避免与对话历史耦合
- **与 is_meta 的关键区别**:is_meta 被 normalize 过滤(中断通知),is_reminder 必须进 API 且排最前——CC 里 reminder 是 isMeta 但**不**被过滤,我们现有 is_meta 承担了过滤语义,复用会破坏契约,所以加独立字段

### 3. usage 锚点估算(pi 同款)

`estimate_context_tokens` 从尾部找最后一条带 usage 的 assistant——server 的 usage 是**精确值**;锚点之后通常只有几条工具结果,chars/4 估算误差 <5%。**从不调 API 估上下文**。密集 JSON(chars/2)启发式来自 pi estimateTokens。摘要请求走 `model="compact"` 指针(辅助请求失败自动回退 main,现成机制)。

### 4. 压缩 = 追加一条摘要消息(append-only 不变量)

压缩**不删除**历史消息:会话 JSONL 保持 append-only,压缩只是追加一条 `is_compaction_summary` 消息,内存中的消息流替换为 `[summary, *retained]`。收益:
- 历史完整可审计(CC 的 entry 链同思想)
- `--continue` 重放时摘要自动定位,UPDATE 迭代模式(第二次压缩把上次摘要给模型更新,不重写)
- normalize 规则 5 保证摘要永不与相邻 user 合并——「历史压缩段」的边界不稀释

### 5. cut 点语义:turn 配对完整性(PI-05 核心价值)

合法 cut 点 = user/assistant/summary 消息边界,**tool_result 承载者永不作为 cut 点**;cut 在 user 上 → 整 turn 保留;在 assistant 上 → 拆 turn,turn 前缀(真实 user 输入 + 工具往返)单独摘要,让保留的 assistant 回复仍「读得懂自己在回答什么」。防压缩后模型看不懂半截对话。

### 6. fileOps:压缩后恢复的数据来源

从被压缩消息提取 Read/Write/Edit 的 file_path,**只统计实际成功执行的调用**(被拒/失败/挂起跳过——恢复读回不得绕过权限闸,review 修复);最新优先、跨轮合并去重,以 `<read-files>/<modified-files>` 标签追加在摘要尾部(随 --continue 存活)。压缩后最近 ≤5 个修改文件内容作为一次性 reminder 注入下一轮请求——模型不会忘了自己改到哪。

### 7. 熔断与防抖

- 连续 2 次摘要失败 → `compaction.enabled = False`(不再每轮烧一次摘要调用)
- `_last_compact_turn` 防抖:thinking-only 重试 `turn -= 1` 同轮重入检查点时,不二次压缩
- 摘要请求不计入 turn(独立调用路径)

### 8. 旧结果清理:请求视图投影

>60 条或距上次清理 30 分钟时,把白名单(Read/Bash/Grep/Glob)旧 tool_result 替换为占位符,保留最近 20 条。**只改请求视图,不动会话日志**——等价 CC microcompact 时间路径,不做 cache_edits 热路径。运行两处:turn 顶部检查点在 autocompact 决策**之前**(CC 注释 "Apply microcompact before autocompact",清理后估算决定是否值得烧一次 LLM 摘要);请求组装时再投影一次(压缩关闭时消息只增长,清理最有用)。

## 风险边界(规格 §5 落地情况)

- **摘要质量不可自动化验证**:VCR 集成测调用形状;质量靠真实 API 抽样(DEEPSEEK_API_KEY)
- **窗口可配**:`CompactionConfig(window=...)`,默认 128K 兜底,不硬编码(profile 后续阶段接 context_window)
- **AGENTS.md 不参与权限**(设计不变量 #3 不变)

## 测试

- `tests/engine/test_compaction.py`(31):cut 点边界(不切 tool_result/整 turn/split turn/空段拒绝)、serialize 截断、摘要单段/UPDATE/split 两次请求/错误传播、fileOps 提取/合并/解析、恢复注入(最近 5/跳过缺失/截断)、清理(计数/时间/白名单/keep_recent=0)
- `tests/engine/test_loop.py`(+12):reminder 前置/上限/不持久化、压缩触发/防抖/熔断/持久化/resume 重放、恢复一次性注入、清理投影不进会话
- `tests/engine/test_tokens.py`(11)、`tests/engine/test_context.py`(12)、`tests/core/test_normalize.py`(+8)
- 全量:**471 passed, 9 skipped**(集成无 key skip)
