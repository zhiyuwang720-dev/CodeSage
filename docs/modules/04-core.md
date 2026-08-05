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

## 生产级强化(2026-08-05)

三轮修复(对照 Kode 审查,测试 170 → 337):

**修复内容**(批次 1 core):
- [高] normalize 对齐 Kode wire 语义:toolResultsFirst 合并序 + assistant 同 id 合并 + 空内容哨兵 —— 此前与 Kode 的 `normalizeMessagesForAPI` 有偏差
- [中] 会话 key 项目作用域(`sanitized(cwd)/` 前缀)—— 不同项目同名会话不再碰撞

**文件级判定**:
- A 类:为空 ✅ —— 04 原设计即达标,文件级审查无 A 类项,仅做了行为对齐
- B 类(映射阶段 X):系统提醒/上下文分层(08)、压缩(10)、会话生命周期(12)
- C 类(理由):无直接命中;多进程文件锁等并发场景归阶段 12,单写者假设仍成立

**现状**:及格 → 良好。normalize 语义与 Kode wire 对齐、会话带项目隔离;剩余能力(摘要/fork/resume)全部在 B 类路线图,存储 schema 已兼容。

## 设计决策剖析

### 为什么这么设计

1. **SessionMessage = AI 契约 + 会话元数据(单类,不继承)**:ai.Message 是"给模型看的",会话消息是"对话里发生的"。同形不同职责,to_ai_message() 显式向下转换,引擎循环在两个形状间显式往返,不隐式混用。Kode 用 User/Assistant/Progress 联合类型;Python 单类 + role 字段 + 元数据位(is_error/is_meta)更简单,序列化/回放/演进都省心。
2. **normalize_for_api 对齐 Kode wire 语义**:丢弃 is_error/is_meta;空白文本清理、空内容补 "(no content)" 哨兵;相邻同角色合并,合并后的 user 消息内 tool_result 块重排到文本前(toolResultsFirst) —— 文本永不与 tool_result 跨消息混,是归一化最高优先级不变量(有专门测试)。
3. **会话 = append-only JSONL + fsync**:会话是"只增"自然模型,append 是唯一写路径(追加 + flush + fsync);load 全量回放,坏行跳过永不致命;恢复/压缩(10)/fork(12)全部基于回放,不需要快照一致性。
4. **项目作用域隔离**:会话文件落在 root/<sanitized(cwd)>/ 下,不同项目同名 session_id 不再碰撞;sanitize 用可读替换(非字母数字 → "-")而非哈希,目录可人眼识别。
5. **手动序列化桥接 dataclass/pydantic**:asdict 只递归 dataclass,ContentBlock 是 pydantic —— to_dict 必须手动 model_dump()。这是两套对象模型混用边界的教训,阶段 12 扩展存储 schema 时同类处理。

### 设计原则

- **单一写入路径**:append 是唯一写入口,无原地修改
- **持久性先行**:fsync 后才返回,崩溃不丢已确认消息
- **损坏容错**:坏行跳过、未知 role 跳过 —— 一个坏消息不能毁掉整个会话
- **显式转换,不隐式混用**:to_ai_message() / normalize 是仅有的跨界路径
- **稳定标识**:uuid 是消息级稳定 ID,归一化/UI/fork 都靠它

### 优点

- 回放即恢复:会话状态完全由日志重放还原,无需额外快照/索引
- 单行单消息 + 跳过容错:崩溃瞬间的 torn 行不致命
- 与 ai 契约同形:引擎循环直接消费,无形状转换摩擦
- 项目隔离 + 兼容查找:list_sessions/find_session/most_recent_session 对两层级透明

### 为什么不选用别的技术方案

| 备选方案 | 为什么不选 |
|---|---|
| SQLite 存会话 | 引入依赖 + 事务对单写者多余;JSONL 人类可读、零依赖、坏行容错天然 |
| 每次整体重写文件 | O(n) 且崩溃丢整文件(或需原子写兜底);append 单写路径更简单 |
| Kode 式多类联合类型 | Python 单类 + role 表达同样语义,序列化/回放/兼容演进更省 |
| 消息索引/按 uuid 查询 | 会话规模小,全量回放 O(n) 足够;索引引入一致性负担 |
| 文件锁 | 单写者假设(CLI 唯一进程);多客户端场景(阶段 12)再加 |
| 会话层也用 pydantic | 契约层需要校验(provider 数据不可信);会话层内部纯数据,slots dataclass 更轻 |

### 技术点清单

append-only JSONL + fsync、损坏行容错回放、normalize 三规则(toolResultsFirst 不变量)、dataclass/pydantic 混用序列化桥接、项目作用域 sanitize、uuid 稳定 ID

## 面试问题整理

### 面试问题与答案

**Q: 会话为什么用 append-only JSONL 而不是每次整体重写文件?**
**A:** 会话是"只增"模型:append 是唯一写路径(追加 + flush + fsync),load 全量回放。恢复、压缩(阶段 10)、fork(阶段 12)全部基于回放,不需要快照一致性。整体重写:每次增长 O(n)、写一半崩溃会丢整个文件。JSONL 单行一个消息,崩溃瞬间的半个 append 只是最后一行坏行,回放时跳过 —— 结构上天然抗 torn write。
**深度衍生: 每个消息都 fsync 不慢吗?** → 每次 fsync 约毫秒级(HDD ~10ms),但会话写频率低(每轮对话几条),延迟可接受;换来"返回即持久"的强语义 —— 进程崩溃不丢已确认消息。这是写入量与持久性的校准:CLI 交互写量天然小,选强语义。
**广度衍生: 与数据库 WAL / Kafka 日志有何异同?** → 同属 append-only 日志范式:顺序写最快、崩溃后重放恢复。Kafka 靠多副本 + 批量刷盘换吞吐;这里单副本 + 每行 fsync 换简单与强持久。规模差异:会话日志无需段压缩,摘要(compact)在阶段 10 作为上层叠加。

**Q: normalize_for_api 为什么拆/合并消息?tool_result 为什么是"重排"而不是"拆成独立消息"?**
**A:** wire 语义:OpenAI 要求 tool 消息与 assistant tool_calls 严格相邻,混文本破坏关联;Anthropic 支持 inline 多块但 tool_result 语义独立。规则:丢弃 is_error/is_meta;清理空白文本,空内容补 "(no content)" 哨兵;相邻同角色合并。2026-08-05 对齐 Kode 后:合并后的 user 消息内 tool_result 块重排到文本前(toolResultsFirst) —— 一条 user 消息可带 [tool_result..., text...](Anthropic-valid inline 格式),消息数最少;OpenAI 侧展开成 role=tool 消息由 adapter 在边界做。
**深度衍生: 合并顺序与 prompt 缓存有什么关系?** → 合并相邻同角色消息(连续 user 文本合成一条)稳定送 API 的消息形状:前缀不变,Anthropic/DeepSeek 的缓存命中率更高;拆碎消息会让每次请求前缀变动、缓存失效 —— 归一化不只为了正确性,也为了成本。
**广度衍生: 归一化层与协议栈的适配层有何共通?** → 都是"把内部语义映射到对端 wire 语义"的边界:像 HTTP/2 多路复用(内部流 → 对端帧)、ORM 方言层。关键设计一致:语义差异锁死在边界,内部保持单一形状。

**Q: SessionMessage 序列化为什么不用 dataclasses.asdict?**
**A:** asdict 只递归 dataclass;content 里的 ContentBlock 是 pydantic BaseModel,asdict 会原样返回对象引用,不可 JSON 序列化。to_dict 必须手动:list[ContentBlock] → [b.model_dump()],Usage 同理;from_dict 反向构造并容错(未知 role 返回 None、缺 uuid 重新生成、缺字段取默认)。这是 dataclass(会话层)与 pydantic(契约层)混用边界的桥接,阶段 12 扩展存储 schema 是同类教训。
**深度衍生: 为什么会话层用 dataclass 而契约层用 pydantic?** → 契约层在 provider 信任边界,需要校验 + Literal 约束 + model_validate(网络数据不可信);会话层是内部纯数据,dataclass(slots=True) 轻量、无校验开销、属性访问快。混用的代价就是 to_dict/from_dict 显式桥接,而不是隐式魔法。
**广度衍生: 从 from_dict 的容错看 schema 演进策略?** → 老文件缺新字段 → 默认值;未知 role → 跳过;uuid 缺失 → 重新生成。这是无 schema 强制(JSON)下的向后兼容套路:读取端兜底、写入端单调新增 —— 与数据库 migration 的"容忍缺失 + 默认值"同思路,只是靠代码而非 ALTER TABLE。

**Q: 会话为什么要按项目作用域隔离?**
**A:** 强化后(2026-08-05)会话文件落在 root/<sanitized(cwd)>/ 子目录,sanitize 把非字母数字替换为 "-" 并 strip 首尾 —— 不同项目里同名 session_id 不再碰撞(此前所有会话平铺在 root 下)。list_sessions 递归两个层级按 mtime 倒序,find_session 按 stem 匹配,most_recent_session 返回最新 —— 全部兼容新布局。
**深度衍生: sanitize 为什么不直接哈希项目路径?** → 可读性:目录名可人眼识别是哪个项目。strip 处理首尾分隔符;全符号路径(如 "/")得到空串时回退根级。哈希唯一性强但不可读、调试痛苦;路径语义 + 低碰撞率足够。
**广度衍生: 与多租户数据隔离有何区别?** → 同是"按归属划分存储防同名碰撞":多租户按 tenant_id 分库分表,这里按项目分目录。区别:这里是弱隔离(同进程可访问全部会话),解决"混淆"而非"越权" —— 安全边界由权限引擎(阶段 05)承担,存储只负责不混淆。
