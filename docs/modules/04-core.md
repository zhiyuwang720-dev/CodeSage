# 阶段 04 — 消息与会话模型理解文档

> 分支 `feat/04-core`,规格见 `docs/specs/04-core.md`。

## 模块职责

core 是领域层地基,三件事:会话级消息类型、送 API 前的归一化、会话持久化。**它不做什么**:不做循环(06)、不做权限(05)、不做摘要(10)、不做 fork/resume(12) —— 但后三者全都站在它上面。

## 设计:会话消息 = AI 契约 + 会话元数据

阶段 02 定了「全系统唯一消息形状」(ContentBlock/Message),阶段 04 在其上加一层会话语义:

```python
SessionMessage(
    role,                    # user | assistant
    content,                 # str | list[ContentBlock] —— 与 AI 契约同形
    uuid,                    # 稳定 ID(归一化、UI、未来 fork 都靠它)
    timestamp,
    usage / model / is_error / is_meta   # assistant 元数据
)
```

**为什么不是继承**:AI 契约(ai.Message)是「给模型看的」,会话消息是「对话里发生的」。两者同形但职责不同 —— `to_ai_message()` 提供向下转换,引擎循环(06)在两者之间显式往返,不隐式混用。

## 关键设计决策

### 1. normalize_for_api:三条规则(Kode normalizeMessagesForAPI 的移植)

送模型前的历史清洗(对应 Kode message-utils/api.ts):

1. **丢弃** `is_error`(提供商错误消息)与 `is_meta`(合成通知)—— 模型不该看到这些
2. **tool_result 独立成消息**:含 tool_result 的 user 消息拆成「文本 user」+「tool_result user」两条。**为什么**:OpenAI 要求 tool 消息与 assistant(tool_calls)严格相邻,Anthropic 也要求 tool_result 语义独立;混合文本会让两家的 wire 转换都不可靠
3. **相邻同角色合并**:连续 user 文本合并成一条、连续 assistant 合并成一条(减少消息数、稳定前缀缓存)

不变量:**tool_result 消息永不与文本合并** —— 这是归一化里最重要的一条,有专门测试。

### 2. 会话 = append-only JSONL

- 一个会话一个 `.jsonl` 文件,`append()` 是唯一写路径(追加 + flush + **fsync**)
- `load()` 回放;损坏行跳过,永不致命 —— **一个坏行不能毁掉整个会话**
- 单写者假设(CLI 是唯一进程)—— 文件锁在需要多进程时再补(设计笔记 #14 的完整形态:JSON + 原子写 + 锁)

**为什么 append-only 而不是整体重写**:会话是「只增」的自然模型 —— 恢复/压缩(10)/fork(12)都能基于回放,不依赖快照一致性。Kode 的会话日志、记忆、任务全部同一套路。

### 3. 序列化用 asdict 的教训

`dataclasses.asdict` 只递归 dataclass,ContentBlock 是 pydantic 对象 —— **to_dict 必须手动转换**(`b.model_dump()`)。这是 dataclass 与 pydantic 混用的边界,阶段 12 扩展存储 schema 时要注意同类问题。

## 与 Kode 的对照

| CodeSage | Kode | 差异 |
|---|---|---|
| SessionMessage(单类) | UserMessage/AssistantMessage/ProgressMessage 联合类型 | Kode 用 union 表达类型安全;Python 用单类 + 元数据字段,role 区分 |
| normalize 三条规则 | normalizeMessagesForAPI + reorderMessages | reorder(progress 归位)留阶段 06;04 只做过滤/合并/拆分 |
| append + fsync | JSONL + 文件锁(10-30s 过期) | 单写者假设;锁留多进程场景 |
| 无 summary | summary 挂 leafUuid | 摘要机制阶段 10,存储 schema 已兼容(消息级 uuid) |

## 已知简化(ponytail)

- 无 ProgressMessage(阶段 07 UI 才需要瞬态占位)
- 无文件锁(单写者);并发写同一会话会交错 —— 阶段 12 多客户端场景再加
- 无消息索引/按 uuid 查询(load 全量回放,O(n);会话规模小,够了)

## 完成标准(对照规格)

- [x] SessionMessage 定型,roundtrip 无损(含 Unicode/工具块/usage)
- [x] normalize 三条规则全测覆盖(10 项)
- [x] Session append-only 追加/回放/损坏容错
- [x] 102 项全量单测绿

## 阶段衔接

- 阶段 05(权限):`SessionMessage` 是权限决策上下文的一部分(会话状态存取)
- 阶段 06(engine):`normalize_for_api` 是每次 LLM 调用的前置步骤;`Session.append` 是循环的持久化副作用
- 阶段 10(compact):summary 机制在 append-only 日志上叠加
- 阶段 12(session):fork/continue 在 uuid + JSONL 之上
