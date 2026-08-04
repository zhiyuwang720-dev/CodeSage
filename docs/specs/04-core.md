# Spec: 阶段 04 — 消息与会话模型

> 分支:`feat/04-core`。依据主规格 `docs/specs/codesage.md`(阶段 04)。

## Objective

core 包是 harness 的领域层地基:会话级消息类型(在阶段 02 内部契约上叠加会话元数据)、送 API 前的归一化(设计笔记 #12/#14)、append-only JSONL 会话存储。引擎(06)、权限(05)、任务(11)、会话生命周期(12)全部基于这层。

## 对照保留清单

- #12 内部消息形状统一:本阶段在其上定义**会话消息**(+uuid/usage/model/is_error)
- #14 持久化 = JSON 文件 + 原子写;会话 = **append-only JSONL**,恢复保摘要前 2 条 user 消息(摘要机制在 10/12,存储基础在本阶段)
- Kode normalizeMessagesForAPI 规则:丢弃 API-error 消息、相邻同角色合并、**tool_result 独立成 user 消息**(Anthropic 语义,OpenAI 侧展开)

## 范围

**做**:
1. `SessionMessage`:会话消息 = ai.Message + uuid + usage/model/is_error/timestamp
2. `normalize_for_api`:过滤 is_error、相邻同角色合并、tool_result 拆分
3. `Session`:append-only JSONL(追加 + fsync)、load 回放、损坏行容错跳过

**不做**:ProgressMessage 瞬态占位(阶段 07 UI);reorderMessages(阶段 06);summary/leafUuid 摘要(阶段 10);fork/resume/归档(阶段 12);消息级文件锁(单写者假设)。

## 项目结构(本阶段新建)

```
codesage/codesage/core/
  __init__.py
  messages.py      # SessionMessage + 工厂
  normalize.py     # normalize_for_api
  session.py       # Session(JSONL 存储)
tests/core/
  test_messages.py
  test_normalize.py
  test_session.py
```

## Commands

```bash
pytest tests/core/ -q
```

## Code Style

主规格风格;SessionMessage 用 dataclass(与 ai.types 的 pydantic 互补:存储层轻量)。

## Testing Strategy

- normalize:过滤/合并/拆分规则逐条断言(相邻 user、相邻 assistant、tool_result 独立、is_error 剔除)
- session:append/load roundtrip、多轮追加、损坏行跳过、UTF-8 内容保真

## Boundaries

- **Always**: 追加写后 fsync;uuid 稳定可复现(测试用 seed)
- **Ask first**: 改变会话存储格式(JSONL schema 变更)
- **Never**: 存储层做业务逻辑;在 normalize 里改消息语义(只合并/过滤)

## Success Criteria

- [ ] SessionMessage 定型,序列化 roundtrip 无损
- [ ] normalize_for_api 规则全测覆盖
- [ ] Session append-only 追加/回放/损坏容错
- [ ] 全量单测绿(含既有 84 项)
