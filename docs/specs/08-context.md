# 阶段 08:上下文工程(Context Engineering)

> 参考:`docs/reference/context-engineering.md`(Claude Code 上下文工程全景)+ `docs/pi-agent-core-analysis.md`(pi 对照)。本规格吸收双方设计,裁剪到 CodeSage 的规模与供应商现实(DeepSeek/Anthropic 双 adapter、无 feature-flag 基建、无服务端 API 专有能力)。

## 0. 验收标准(tasks/todo.md 阶段 08 条目)

- [ ] AGENTS.md 逐层收集 + 32KB 截断 + override
- [ ] system prompt 分层组装(静态 base 与动态 context 分离)
- [ ] system-reminder 注入(上限 10)
- 
- [ ] CC-12 context 改 reminder 注入;CC-13 上下文 memoize + 失效;CC-14 git status 快照
- [ ] PI-05 结构化 compaction(usage 优先估算 + turn 边界 + split-turn 前缀摘要)

## 1. 三方对照与裁剪决策

| 机制 | Claude Code | pi | 我们现状(07) | 08 采取 |
|---|---|---|---|---|
| 上下文组装 | system 静态块 + messages[0] reminder + 附件消息 | buildSessionContext(entry 投影) | system prompt 内嵌 context 标签,**每轮重建** | 拆两层:静态 system base + reminder 注入(CC-12) |
| 组装缓存 | memoize + mtime 失效 | 每轮投影(轻量) | 每轮重建(含子进程!) | memoize 每会话一次(CC-13) |
| git 上下文 | 快照 + 2K 截断 + disclaimer | 无 | 无 | git 快照(CC-14,可选层) |
| token 估算 | usage 锚点 + chars/4 | 同左,image=4800 | **无** | tokens.py(pi 同款) |
| 触发阈值 | effectiveWindow − 13K | window − reserve(16K) | 无 | pi 式 should_compact(window − 16K) |
| 压缩流水线 | 五级(预算→snip→micro→collapse→auto) | 单级 autocompact(显式 API) | 仅工具结果 spill(>100K 落盘) | **三级裁剪**(见下) |
| 摘要质量 | analysis+summary 两段、9 节 | 8 节结构化 + UPDATE 迭代 + 文件操作列表 | 无 | pi 式 8 节 + fileOps(不需要两段:成本 ×2) |
| 摘要请求 | fork 子 agent | 独立请求(cacheRetention none) | 有 compact 指针未用 | 走 compact 指针 + 失败回退 main(现成) |
| cut 边界 | turn 边界(拆 turn 前缀单独摘要) | 同左(合法 cut 点) | 无 | pi 式 find_cut_point |
| 压缩后恢复 | 最近 5 文件 + 技能 | fileOps 列表注入摘要 | 无 | pi 式 fileOps + 最近读文件重注入 |
| 旧结果清理 | microcompact(时间/计数 + cache_edits) | 无 | 无 | **简化版**:计数/时间触发占位符(无 cache_edits) |
| 熔断 | 连续 3 次失败停 | 无 | 无 | 连续 2 次失败停(auto 路径) |

### 裁剪决策(与理由)

1. **五级 → 三级**。L4 Context Collapse(投影式折叠)需要常驻跟踪 + 与 autocompact 竞争阈值,是为服务端缓存场景设计的复杂机制;我们上下文规模小、模型窗口大(DeepSeek 128K/Anthropic 200K),折叠的收益不抵复杂度。L2 History Snip 与 L3 合并为"旧 tool_result 清理"一步。
2. **不做 cache_edits / API 端 context management**。Anthropic beta API,DeepSeek 无对应;服务端前缀缓存(Anthropic 显式 cache_control、DeepSeek 自动)我们只做"客户端不主动破坏前缀"的被动配合(reminder 前置、system 静态化、摘要请求不污染缓存)。
3. **摘要 prompt 不用 CC 的 analysis+summary 两段**。两段式多花一次输出预算且要丢弃 analysis;pi 的单段结构化 8 节格式在实测中足够,且支持 UPDATE 迭代模式(增量压缩不用重读全历史)。
4. **压缩触发走显式检查点而非后台监控**。pi 把 compact 做成宿主调用的显式 API;我们在 loop 的 turn 顶部检查点调 should_compact——与现有 abort/max_turns/max_budget 三检查点同构,零新增并发。

## 2. 总体架构

```
┌────────────────────────────────────────────────────────────────────┐
│  会话一次(CLI 启动)          每轮(loop 顶部检查点)                   │
│  ┌─────────────┐             ┌──────────────────────────────┐      │
│  │ context.py  │──memoize──▶ │ ① 检查点:abort/max_turns/     │      │
│  │ AGENTS.md   │             │    max_budget + ② should_     │      │
│  │ git 快照    │             │    compact(tokens.py 估算)    │      │
│  │ 日期        │             └──────────┬───────────────────┘      │
│  └─────────────┘                        │ 超阈值                    │
│                                         ▼                          │
│                                ┌──────────────────────────┐        │
│                                │ ③ compaction.py          │        │
│                                │  find_cut_point(不切turn) │        │
│                                │  summarize(compact 指针)  │        │
│                                │  摘要消息替换历史 + 持久化 │        │
│                                └──────────────────────────┘        │
│                                                                     │
│  组装(每轮 _ask_model)                                              │
│  ┌────────────────────────────────────────────────────────┐        │
│  │ system: base(静态,字节稳定)                             │        │
│  │ messages: [0] <system-reminder> context(会话内不变)     │        │
│  │            [1..] 历史消息(含摘要消息)                   │        │
│  └────────────────────────────────────────────────────────┘        │
└────────────────────────────────────────────────────────────────────┘
```

### 消息结构(API 视角)

```
system    = base 提示词(静态;context 不再内嵌)
messages  = [reminder 用户消息(is_reminder)      ← context bundle 组装一次
             用户消息/助手消息/tool_result...     ← 对话历史,压缩时被摘要替换
             摘要消息(is_compaction_summary)      ← 压缩产物,替换被压缩段落]
```

## 3. 分模块设计

### 3.1 消息契约扩展(`core/messages.py` + `core/normalize.py`)

**SessionMessage 新增两个布尔字段**(to_dict/from_dict 同步):

- `is_reminder: bool = False` — system-reminder 注入消息(AGENTS.md/git 快照/日期等上下文载体)。**语义与 is_meta 不同**:is_meta 被 normalize 过滤(中断通知等,不进 API),is_reminder 必须进 API 且固定在消息流最前。
- `is_compaction_summary: bool = False` — 压缩摘要消息(role="user",content=摘要文本)。normalize 时与普通 user 消息同等对待,但**禁止与相邻 user 消息合并**——它代表"历史压缩段",合并会稀释边界(pi: compaction entry 在 context 投影中保持独立)。

**normalize_for_api 更新**(`core/normalize.py`):

1. 过滤不变(is_error/is_meta 丢弃)
2. **reminder 稳定前置**:所有 is_reminder 消息在输出中排最前、相互合并为一条(相邻 reminder 之间不插入其他消息)——保证 system 之外的前缀字节稳定,配合服务端前缀缓存;合并后内容顺序 = 组装顺序
3. summary 消息保留原位置,不与相邻 user 合并(第 2 条规则跳过它)
4. 现有规则(空块清理、toolResultsFirst、同角色合并)不变

**理由**:is_reminder 与 is_meta 分离是 CC 的实体映射——CC 中 reminder 是 `isMeta` 用户消息但**不**被过滤(过滤的是 `isVirtual`)。我们现有 is_meta 承担了过滤语义,不能复用,加独立字段是唯一不破坏现有过滤契约的方式。合并相邻 reminder 借鉴 `smooshSystemReminderSiblings`:减少 user 轮次边界,避免 API 交替规则风险。

### 3.2 Token 估算(`engine/tokens.py`,新)

```python
def estimate_tokens(content: str | list[ContentBlock]) -> int   # chars/4;JSON 密集 ×2;image=4800
def estimate_message_tokens(m: SessionMessage) -> int
def estimate_context_tokens(messages: list[SessionMessage]) -> ContextEstimate
    # 从尾部找最后一条带 usage 的 assistant(跳过 is_error)——usage 锚点;
    # 锚点之前 = usage 汇总(server 精确值);之后逐条估算
def should_compact(tokens: int, window: int, reserve: int = DEFAULT_RESERVE) -> bool
    # tokens > window - reserve
```

常量:

| 常量 | 值 | 出处 |
|---|---|---|
| CHARS_PER_TOKEN | 4 | pi estimateTokens / CC tokenCountWithEstimation |
| ESTIMATED_IMAGE_CHARS | 4800 | pi |
| DEFAULT_CONTEXT_WINDOW | 128000 | 兜底(profile 可配 context_window) |
| DEFAULT_RESERVE_TOKENS | 16384 | pi DEFAULT_COMPACTION_SETTINGS.reserveTokens |

**理由**:usage 锚点策略(CC 与 pi 一致)是"从不调 API 估算上下文"的关键——server 的 usage 是精确值,锚点后的新增消息通常只有几条工具结果,chars/4 误差 <5%。image 按固定 4800 chars 而非内容,因为图片内容不可估算(pi 同款)。

### 3.3 上下文构建(`engine/context.py`,新)

```python
@dataclass
class ContextBundle:
    sections: list[tuple[str, str]]   # (标题, 内容)——每条渲染为 reminder 一段
    # 固定顺序:日期 → git 快照 → AGENTS.md(近→远)

def build_context_bundle(cwd: Path, *, override_file: Path | None = None) -> ContextBundle
```

**AGENTS.md 逐层收集**:

1. 从 cwd 向上遍历到文件系统根,每层收集 `AGENTS.md`(与 CC 的 `CLAUDE.md` 对应;本阶段不做 `.claude/AGENTS.md` 与 `rules/` 目录,留待需要)
2. **优先级近→远**:靠近 cwd 的追加在末尾(近因效应,LLM 对末尾关注度更高);与 CC 相同
3. **32KB 总预算**:各文件按 远→近 依次计入;超预算时截断最远文件内容,再超则丢弃更远文件(远的价值最低)
4. **override**:`override_file` 指定时,完全替代自动收集的 AGENTS.md 部分(对应 CC 的 `--bare`/显式指定语义;优先级最高的"最近"文件 = override 内容)

**git 快照(CC-14,可选层)**:git 仓库内时收集 branch + 最近 5 条提交 + `status --short`(截断 2000 字符),附 disclaimer「This is the git status at the start of the conversation...snapshot」。git 命令带 `--no-optional-locks`,并行执行;非 git 仓库或命令失败 → 该节省略。

**memoize(CC-13)**:`build_context_bundle` 在 CLI 启动时调用一次,结果对象直接传入 AgentLoop;loop 内零重建。失效策略:AGENTS.md 在会话中修改不重载(会话内一致性优先于新鲜度,与 git 快照的 snapshot 语义一致);新会话自然重建。

**理由**:context 的组装成本主要在子进程(git)与磁盘 IO(AGENTS.md 读取),每轮重建是纯浪费——CC 用 memoize + mtime 失效,我们简化为会话级一次(我们的失效需求与 CC 不同:CC 有 worktree 切换等运行时变更源,我们单进程单目录,会话中无变更源)。

### 3.4 注入接线(`engine/loop.py` 改造)

- `AgentLoop.__init__` 增参 `context_bundle: ContextBundle | None`;删去/弃用旧 `context: dict`
- `_ask_model`:
  - `system=build_system_prompt(base)` — 只含 base,**字节级稳定**
  - 请求消息 = `[reminder_msg] + normalize_for_api(history)`;reminder_msg 由 bundle 渲染(每条 section 包 `<system-reminder>` 段,合计最多 **10 段** —— 验收标准)
  - reminder 消息只进请求,**不持久化**到会话文件(它是派生的,不是对话内容;持久化会污染 resume 重放)

**理由**:system 静态化 + reminder 前置的双重收益——(a) system 前缀字节稳定,Anthropic 端可打 cache_control 断点、DeepSeek 端自动前缀缓存命中;(b) context 变化(后续阶段:记忆、技能)只需追加 reminder 段,不触碰 system 缓存。不持久化 reminder 的理由:resume 时由 bundle 重建,避免与对话历史耦合(pi 同样把 context 投影放在读路径而非写路径)。

### 3.5 压缩(`engine/compaction.py`,新,PI-05 落地)

**触发(loop turn 顶部检查点,接在 abort/max_turns/max_budget 之后)**:

```python
if self.compaction.enabled and should_compact(
        estimate_context_tokens(messages).tokens, window, reserve):
    messages = await self._compact(messages)   # 摘要替换 + 持久化
```

防抖:压缩后标记 `last_compact_turn`,同一轮不再触发;连续 2 次失败 → `self.compaction.enabled = False`(熔断,自动路径)。

**find_cut_point(messages, keep_recent=20000)**:

1. 合法 cut 点 = user/assistant/summary 消息边界,**tool_result 永不作为 cut 点**(配对完整性)
2. 从尾部向前累计 token 到 ≥ keep_recent → 取该位置**之前最近的合法 cut 点**
3. cut 点在 user 消息上 → 整 turn 保留(`is_split_turn=False`)
4. cut 点在 assistant 消息上 → 拆 turn:该 turn 的 user 前缀(从 turn 起点到 cut 点)作为 `turn_prefix` 单独摘要

**摘要生成(generate_summary)**:

- 输入:`serialize_conversation(messages)` 文本化(用户文本 / 助手文本 / thinking / tool calls / tool results,工具结果截断 2000 字符)+ `<conversation>` 包裹
- prompt:pi 的 SUMMARIZATION_PROMPT 8 节(Goal / Constraints & Preferences / Progress(Done/In Progress/Blocked) / Key Decisions / Next Steps / Critical Context);已有摘要时用 UPDATE_SUMMARIZATION_PROMPT 迭代(增量压缩)
- 摘要尾部追加 `<read-files>` / `<modified-files>` 列表(从被压缩消息的 Read/Write/Edit 工具调用提取,跨压缩轮次合并去重 —— pi fileOps)
- 请求:`model="compact"` 指针(现成机制,失败自动回退 main);`max_tokens` 限制为 reserve 的 80%
- split-turn 时:历史段与 turn 前缀段各调一次,拼接

**摘要消息**:

```python
user_message(summary_text, is_compaction_summary=True)
```

写入会话文件(append-only 语义不变:压缩 = 追加一条摘要消息;`--continue` 重放时按 is_compaction_summary 定位,其后消息即为可继续的上下文)。

**理由**:
- **usage 锚点 + turn 边界**:抄 pi,防"压缩后模型看不懂半截对话"(PI-05 核心价值)
- **摘要消息持久化而非原地删除**:遵守会话 append-only 不变量;历史 JSONL 完整保留,可审计可恢复(与 pi entry 链同思想:compaction 是追加一个 entry)
- **UPDATE 迭代模式**:第二次压缩时把上次摘要一并给模型更新,摘要不重写(省 token,信息不丢失)
- **fileOps 追加到摘要**:让压缩后的模型仍知道"改过哪些文件"(CC 的恢复机制的数据来源),成本一次正则扫描,零 API

### 3.6 压缩后恢复(`engine/compaction.py` 内)

压缩完成后,若 fileOps.modified/read 非空:取最近 ≤5 个被修改文件,每个 ≤5K token,内容作为 reminder 段追加注入下一轮请求(标题「Recently modified files」)。文件读失败/超限 → 跳过。

**理由**:CC 的 runPostCompactCleanup 是压缩质量的关键一环——不恢复的话,模型压缩后忘了自己改到哪。我们用自己提取的 fileOps,不需要 CC 的 readFileState 缓存。

### 3.7 旧工具结果清理(`engine/compaction.py` 内,轻量)

触发:`len(messages) > MAX_RESULTS_BEFORE_CLEAN(默认 60)` 或距上次清理超过时间阈值(默认 30 分钟)时,在**组装请求前**(非持久化,仅请求视图)将较早的 tool_result 内容替换为占位符文本(`[Old tool result content cleared — see session log]`),保留最近 20 条。仅对 Read/Bash/Grep/Glob 类结果可清(白名单)。

**理由**:等价于 CC 的 microcompact 时间路径(缓存冷时的直接替换),不做 cache_edits 热路径(见裁剪决策 2)。作为**请求视图投影**而非持久化修改,是为了不破坏会话 append-only 与审计完整性——pi 也没有这层,属于 CC 独有增强的降级版。

## 4. 执行步骤(依赖序)

| # | 步骤 | 内容 | 依赖 | 验收(测试) |
|---|---|---|---|---|
| S1 | 消息契约扩展 | SessionMessage +is_reminder/+is_compaction_summary;normalize 前置/不合并 | — | `tests/core/test_normalize.py`:reminder 前置、summary 不合并、is_meta 仍过滤 |
| S2 | tokens.py | 估算 + 阈值 | — | `tests/engine/test_tokens.py`:usage 锚点、image、JSON 密集、should_compact 边界 |
| S3 | context.py | AGENTS.md 收集/截断/override + git 快照 + 日期 + memoize | — | `tests/engine/test_context.py`:tmp 目录树(多层 AGENTS.md、超 32KB、override、非 git 仓库) |
| S4 | 注入接线 | loop 收 bundle;system 静态化;reminder 消息入请求不入会话 | S1, S3 | `tests/engine/test_loop.py`:请求消息序、会话文件无 reminder |
| S5 | compaction.py 核心 | find_cut_point + serialize + 摘要生成(compact 指针) | S2 | `tests/engine/test_compaction.py`:cut 点边界(不切 tool_result/整 turn/split turn)、serialize 截断;摘要 VCR 集成 |
| S6 | loop 压缩接线 | turn 顶部检查点 + 摘要替换 + 持久化 + 熔断 | S4, S5 | `tests/engine/test_loop.py`:小窗口触发、防抖、熔断、resume 重放 |
| S7 | 恢复 + 清理 | fileOps 提取注入最近文件;旧结果清理 | S5 | 恢复注入单测;清理投影单测 |
| S8 | 收尾 | 常量收敛、memoize 失效说明、docs/modules/08-context.md、todo 勾选 | S6, S7 | 全量回归 |

**依赖图**:S1 → S4;S2 → S5 → S6;S3 → S4;S5 → S7;S6+S7 → S8。(S1/S2/S3 可并行)

## 5. 风险与边界

- **摘要质量不可自动化验证**:VCR 集成测试只能验证调用形状;质量靠真实 API 抽样(测试环境 DEEPSEEK_API_KEY)
- **压缩与 max_turns 交互**:压缩消耗一轮摘要请求,不计入 turn 数(摘要请求走独立调用路径,不递增 turn)
- **DeepSeek 上下文窗口**:profile 需 context_window 字段(默认 128K);Anthropic 200K——设计上 window 可配,不应硬编码
- **AGENTS.md 不参与权限**(设计不变量 #3):context 收集与权限解析是两条独立路径,本阶段不改变该不变量

## 6. 与路线图的关系

- 落地 CC-12(reminder 注入)、CC-13(memoize)、CC-14(git 快照)、PI-05(结构化 compaction)
- CC-16(会话 UX)/ PI-07/08/09(会话生命周期)仍属阶段 12;PI-10(消息分离)辅助本阶段的 reminder 消息类型即其第一步
- 阶段 15(MCP)的指令增量、阶段 17(记忆)的 memdir 注入,均走本阶段的 reminder 附件通道(上限 10 段预留了扩展位)
