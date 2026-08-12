# 阶段 12:session 会话生命周期(树状会话)

> 基于:阶段 04 会话存储(append-only JSONL,已实现)+ 阶段 07 CLI resume 三命令(已实现)+ 阶段 10 压缩(摘要管线已实现,SessionSummaryRecord 裁决迁此)+ pi-agent-core-analysis 树状会话设计(§2,重点参考)+ 用户产品需求(树状结构 /tree 导航 / 单文件多分支 / 类型筛选 / 书签)+ todo.md 42-44 行 + 主规格路线图 153 行。
> 前置规格:`docs/specs/codesage.md`(主规格,路线图 + 保留清单 #14/#15)、`docs/specs/04-core.md`(会话契约,红线「Ask first: 改变会话存储格式」)、`docs/specs/10-compact.md`(§1.2「SessionSummaryRecord 归阶段 12」裁决)。
> 参考实现(只读):pi/packages/agent 树状会话(`docs/pi-agent-core-analysis.md` §2,entry 链 + lane 指针);Kode-CLI `packages/core/src/logging/log/paths.ts`(sidechain 多文件方案,对照后否决)。

## 0. 验收标准(todo.md 42-44 行 + 用户树状需求)

- [ ] **树状会话存储**:会话 JSONL 升级为 typed-entry 格式(`message`/`lane`/`bookmark`/`branch_summary`/`operation`/`model_change`/`meta` 七类),**消息带 parent 链**,**所有分支保存在单个文件中**(lane 指针追加式,对齐 Pi);旧格式文件读取兼容(04 契约红线)
- [ ] **fork/continue/resume 升级**:`--continue` 沿活跃 lane 继续(07 语义保留);`/fork <entryId>` 从任意先前位置分支(新 lane);`--continue --lane <name>` 沿指定 lane;`--resume`/`--session-id` 语义不变
- [ ] **/tree 导航**(用户重点):`/tree` 渲染树(分支/lane/书签标注)、`/tree <entryId>` 导航至任何先前位置并可继续、`/tree --type <消息类型>` 筛选、书签 `✓` 标注
- [ ] **书签**:`/bookmark <entryId> <name>` 标记,/tree 展示,书签持久化在会话文件内
- [ ] **sidechain 日志**:操作记录 entry(PI-07:`operation_started`,kind + 参数)追加式落盘,`--continue` 时检测未完成操作(`find_open_operations`),从中断点恢复而非消息末尾
- [ ] **会话自描述**(PI-08):meta entry(初始模型/思考级别/cwd)+ `model_change` entry(会话内变更),审计/恢复不用猜当时配置
- [ ] **SessionSummaryRecord(10 裁决迁移)**:`branch_summary` entry 挂 leaf uuid(保留清单 #14「summary 挂 leafUuid」),恢复保摘要前 2 条 user 消息(保留清单 #14)
- [ ] **归档与会话选择器**:`/archive <sessionId>` 归档(移入 archive 目录);`/sessions` 列出活跃 + 归档会话(选择器支持,07 `--session-id` 已有,列表补齐)
- [ ] **验证**:树/分支单测(`tests/core/test_tree.py`)+ fork 语义单测 + 操作恢复单测 + 归档单测 + 斜杠命令单测 + 04 会话测试**零改动**回归 + 全量回归 `python -m pytest tests/ -q` 全绿(基线 918,2026-08-08)

## 1. 目标与范围

### 1.1 做什么(12 主要做什么)

阶段 12 把会话从**扁平消息流**升级为**树状历史 + 生命周期管理**。先回答「什么已经做了」:07 已交付线性版会话入口 —— `--continue`(同文件重放 + 追加)、`--resume`(摘要 + 新会话)、`--session-id`(指定恢复,`cli/__init__.py:72-76,128-165`);04 已交付 append-only JSONL 存储。**12 不做的事是把这些重做一遍,而是在其上叠加树状分支与生命周期能力**:

| 需求 | 来源 | 落位 |
|---|---|---|
| 树状会话(单文件多分支、/tree 导航、类型筛选、书签) | **用户明确要求** + PI-09 + pi-agent-core-analysis §2 | §3-§6 |
| fork/continue/resume 升级 | 主规格 153 行 + todo.md 42-44 | §5 |
| sidechain 日志 | 主规格 153 行(todo 原话)+ PI-07 | §7 |
| 会话自描述(模型/思考级别/活动工具) | PI-08 | §8 |
| 归档 | 主规格 153 行 | §9 |
| 会话选择器支持 | 主规格 153 行 | §9 |
| SessionSummaryRecord 稳定摘要 | 10-compact.md:50 裁决迁移 + 保留清单 #14 | §4.5 |
| 会话 UX 元数据(标题/标签) | CC-16(plan.md「立即做」小项) | §8.3 |

### 1.2 不做什么(候选裁剪裁决表)

| 候选 | 来源 | 裁决 |
|---|---|---|
| sidechain **多文件**分支(`-fork-{n}-sidechain-{m}.jsonl`) | Kode paths.ts | ❌ **否决,用户指定单文件**。Kode 每次 fork 新建文件,历史分散;用户明确「所有分支保存在单个文件中」= Pi lane 指针方案。单文件 = fork/回滚/书签全部追加,历史永不丢失,文件数恒定 |
| forkContext 读父会话 / resume 从转录缓存恢复 | 保留清单 #15 | ❌ 归 13 子代理(forkContext 是子代理语义,转录缓存与 13 的前/后台相关)。12 只做 fork 的**存储与入口**,13 复用同一 fork API |
| PI-10 AgentMessage 物理分离(应用状态与模型上下文物理分离 + convertToLlm 边界) | pi-agent-core-analysis.md:105 PI-10 | ⚠️ **部分采纳**:12 的 entry 模型天然分离(§3.2 应用状态类 entry 永不进模型上下文,只被读取器消费);但**不重构** SessionMessage 消息类型本身(04 契约,消息仍是 `role/content` 直通 LLM)。convertToLlm 边界重构归 04 模块,12 不动 |
| 交互式会话选择器 UI(列表选择菜单) | 主规格「会话选择器支持」 | ❌ 12 只交付 `/sessions` 列表命令 + 既有 `--session-id`;交互式选择器是 REPL UI 渲染层的事(与 11 §1.2「UI 归阶段 12」口径一致 —— 本轮先交付数据面,UI 面留 07 cli 后续强化,注释 ponytail) |
| 粘贴引用化(paste-cache,>1KB 哈希外置) | CC-16 | ❌ 独立 UX 小项,不属会话生命周期;裁剪,后续评 |
| 会话级文件锁 | 04 session.py:6「single-writer assumption:file locking arrives with multi-process needs」 | ❌ 仍单写者(CLI 单进程);多进程并发在 13 多代理时按需加,格式先行预留(§3.5 追加兼容) |
| 自动归档(按天数/大小触发) | — | ❌ 只做手动 `/archive`;自动策略是 UX 偏好,裁剪 |
| operation 配对 end 记录(operation_completed/operation_failed) | Kode findOpenOperations 完整语义 | ⚠️ 12 只记 `operation_started`(单向),中断检测 = 「最后操作无后继 user 消息」启发式(§7.3);配对 end 需要引擎侵入式埋点,归后续强化,见 R6 |

### 1.3 三分法边界

- **已有,12 复用**:`core/session.py`(Session append/load/exists,扩展而非重写)、`core/messages.py`(SessionMessage 序列化契约,消息 entry 复用它)、`cli/commands.py`(CC-09 斜杠命令注册表,`/tree` `/fork` `/bookmark` `/sessions` `/archive` 直接注册)、`cli/__init__.py` resume 三命令(升级而非重写)、`cli/assemble.py:41-42`(session/history 装配点)、`engine/loop.py`(引擎只消费**线性消息视图**,§4.4 保证零改动)、10 的摘要管线(`engine/compaction.py::generate_summary`,branch_summary 复用)、`config/atomic.py`(归档移动不涉原子写,无变更)。
- **12 新增**:`core/session/` 包(从单文件 session.py 升级:entry 模型 + 树视图 + lane + 操作日志 + 归档;§3.1 包结构)、五个斜杠命令、`cli/__init__.py` 的 `--lane` 参数、`assemble.py` 的 lane 装配。
- **语义微调**(红线,需回归):`core/session.py` 的 `Session` 类**原地升级**(存储格式从「纯消息行」升级为「typed-entry 行」,旧文件兼容读);`Session.load()` 的**返回语义保持**「沿活跃 lane 的线性消息列表」(引擎零改动);`SessionMessage` **零改动**(消息 entry 用 `type` 标记包裹,复用其 to_dict)。

## 2. 核心裁决:单文件 typed-entry JSONL + lane 指针(对齐 Pi,用户指定)

**裁决:会话文件从「纯消息 JSONL」升级为「typed-entry JSONL」,所有分支(消息链 + lane 指针 + 书签 + 摘要 + 操作日志)写在同一文件,append-only 不变。**

理由:

1. **用户明确指定**(产品需求原文):「会话以树状结构存储……所有分支都保存在单个文件中」= Pi 的 entry 链 + lane 指针设计(pi-agent-core-analysis §2)。
2. **追加式永不删除**:分支/fork/compaction/回滚都是追加一个新 entry(对齐 Pi「Entry 追加式,永不删除;compaction 也是插入一个 entry」),04 的 append-only + fsync 契约(设计笔记 #14)原样成立,单写者假设不变,损坏行跳过容错(`core/session.py:53`)复用。
3. **文件数恒定**:一会话一文件,`list_sessions`/`most_recent_session`/`find_session`(core/session.py:62-79)无需感知分支;Kode 多文件方案(fork 后新文件)会破坏会话选择器的文件枚举语义。
4. **与 11 tasks 的关系零交互**:任务存储独立(`{config_dir}/tasks/`),会话文件形状变化不影响任务;fork 出的新分支共享同一 session_id → 共享 taskListId(11 §12 已成文)。

**被拒方案**:

| 方案 | 拒绝理由 |
|---|---|
| Kode 多文件 fork(`-fork-{n}-sidechain-{m}`) | 用户指定单文件;多文件破坏文件枚举语义、历史分散、书签跨文件 |
| 消息行内联 parent 字段(不引入 entry 类型) | 分支摘要/书签/操作日志/模型变更无家可归;`type` 字段是 Pi entry 模型的核心,一行一类型的可筛选性(用户要求「按消息类型筛选」)靠它 |
| 独立 sidechain 目录(`sessions/{id}/sidechains/`) | 单文件裁决的推论:目录方案仍是多文件,同被拒 |

## 3. 存储设计

### 3.1 包结构(04 的 session.py 原地升级为包)

```
codesage/codesage/core/session/        # 12 新建包(04 的 core/session.py 迁移至此)
  __init__.py          # 导出 Session/SessionEntry/EntryType/... 保持 core 包面
  entry.py             # entry 模型:SessionEntry(dataclass)+ 七类工厂 + 序列化
  session.py           # Session 升级:append(entry)/load(线性视图)/tree/fork/bookmark/operations
  tree.py              # 树视图:parent 链构建、lane 解析、按类型筛选、路径渲染文本
  archive.py           # 归档:archive_session/active_sessions/archived_sessions
tests/core/
  test_session.py      # 04 既有,零改动回归(旧格式 roundtrip)
  test_entry.py        # 新增:entry 七类序列化/反序列化、旧行兼容
  test_tree.py         # 新增:树构建/分支渲染/lane 解析/类型筛选
  test_fork.py         # 新增:fork 语义/单文件多分支/线性视图随 lane 变化
  test_operations.py   # 新增:操作日志/未完成检测
  test_archive.py      # 新增:归档/恢复
```

> **迁移裁决**:04 的 `core/session.py`(79 行)迁移为 `core/session/` 包(目录规划规范「每模块一个包,契约层/实现层分层」);`core/__init__.py` 导出面不变(`Session`/`find_session`/`list_sessions`/`most_recent_session` 同签名),全部既有引用(cli/engine/tests)零改动。

### 3.2 entry 模型(七类,对齐 Pi types.ts:14-74)

```python
@dataclass(slots=True)
class SessionEntry:
    type: EntryType          # "message" | "lane" | "bookmark" | "branch_summary"
                             # | "operation" | "model_change" | "meta"
    uuid: str                # 唯一;消息 entry 的 uuid 即其身份(与 04 的 uuid 同源)
    timestamp: str
    parent: str | None       # 前驱 entry uuid(message 链:沿 parent 走);lane/bookmark/meta 无
    data: dict               # 类型特有字段(见下)
```

**七类 entry 契约**(落盘 JSON:`{"type": ..., "uuid": ..., "timestamp": ..., "parent": ..., **data}`):

| type | data 字段 | 说明 |
|---|---|---|
| `message` | 04 `SessionMessage.to_dict()` 全字段(`role`/`content`/`usage`/`model`/…) | 消息本体,复用 04 序列化,零改造;`parent` 指向前一条消息(链) |
| `lane` | `name: str`、`leaf: str`(指向消息 entry uuid) | **分支指针**:活跃 lane = 文件最后一条 lane entry;fork = 追加新 lane entry(§4.2) |
| `bookmark` | `name: str`、`entry: str`(指向被标记 entry) | 书签(用户需求),追加式 |
| `branch_summary` | `content: str`(摘要文本)、`leaf: str`(挂分支 leaf,保留清单 #14「summary 挂 leafUuid」) | 压缩摘要快照(§4.5);compaction 时由 10 的摘要管线产出 |
| `operation` | `kind: str`(`tool_started`/`step_attempt`)、`tool: str | None`、`args_summary: str | None` | 操作日志(PI-07,§7) |
| `model_change` | `to: str`(新模型指针名)、`from: str | None` | 会话内模型变更(PI-08,§8) |
| `meta` | `model`、`show_thinking`、`cwd`、`system_prompt_hash` 等 | 文件首行,会话自描述锚点(§8) |

**应用状态与模型上下文的物理分离**(PI-10 部分采纳):`message` 是唯一进入 LLM 上下文的 entry;`lane`/`bookmark`/`branch_summary`/`operation`/`model_change`/`meta` 是**应用状态**,只被读取器(树视图/恢复/审计)消费,`load()` 线性视图永远只投影 message 链 —— 对齐 Pi「AgentMessage 与应用状态分离,只在 LLM 边界转换」的精神,而不动 SessionMessage 本身。

### 3.3 序列化与旧格式兼容(04 红线)

- 新写入:`json.dumps({"type": "message", **msg.to_dict(), "uuid": ..., "parent": ..., "timestamp": ...})` 一行一条(与 04 同一行 JSON 形状,加 `type`/`parent` 两个键)。
- **旧文件兼容读**(红线:04 会话测试零改动):`load()` 遇到**无 `type` 键**的行 = 04 旧格式消息行 → 视为 `type="message"`,`parent` 推导 = 上一行消息的 uuid(线性链);`type="lane"` 行缺失时(旧文件)默认单 lane `main`,leaf = 最后一条消息。
- 升级策略:**惰性** —— 旧文件不迁移(读取时推导),只有新写入才产生新格式;04 的损坏行跳过容错(session.py:53)不变。
- 04 的 `to_json/from_dict` roundtrip 测试原样通过(消息 entry 复用同一序列化函数,`test_session.py` 零改动)。

### 3.4 写入路径(append-only 契约不变)

```python
class Session:
    def __init__(self, session_id, root, project_key=None):  # 签名不变
        self._lane = "main"          # 活跃 lane 名
        self._cursor = None          # parent 游标(上一条消息 uuid;load 时重建)
        ...
    def append_message(self, msg: SessionMessage) -> SessionEntry:
        """唯一消息写入面:写消息(挂 parent 游标)后**顺带追加同名校验 lane 指针
        entry**(leaf=新 uuid)推进活跃 lane —— 引擎只调它,不感知指针(§4.2 机制)"""
        entry = make_message_entry(msg, parent=self._cursor)
        self._append(entry)
        self._append(lane_entry(name=self._lane, leaf=entry.uuid))
        self._cursor = entry.uuid
        return entry
    def append_lane(self, name: str, leaf: str) -> SessionEntry:
        """fork 用:追加新 lane entry 并重置游标(活跃 lane=name,parent 游标=leaf)。"""
        self._lane, self._cursor = name, leaf
        return self._append(lane_entry(name=name, leaf=leaf))
    def append_bookmark(self, name: str, entry: str) -> SessionEntry: ...
    def append_operation(self, kind, tool=None, args_summary=None) -> SessionEntry: ...
    def append_meta(self, **kw) -> SessionEntry: ...
    def _append(self, entry) -> None:
        with open(self.path, "a", encoding="utf-8") as f:   # 与 04 相同:追加 + flush + fsync
            f.write(entry.to_json() + "\n")
            f.flush()
            os.fsync(f.fileno())
```

**lane 指针推进机制(写死,实现必须遵循)**:每条消息后跟一条同名校验 lane 指针(leaf=该消息 uuid)→ 活跃 lane 的指针恒指向其**最新消息**;fork 的 lane entry 是唯一例外(leaf=分支起点,§4.2)。`parent` 由 Session 内部游标维护(`append_lane` 重置、`load` 时重建)—— 消息链的 `parent` 不依赖调用方传参,防止漏传断链,也保证 fork 后新消息挂 fork 点而非旧游标。

- **损坏行容错不变**:load 跳过坏行;若坏行恰是最后一条 lane entry → 退回上一个 lane(§4.3)。

### 3.5 并发预留(不实现,ponytail 注释)

单写者假设保持(§1.2 已裁);entry 格式每行自包含(uuid/parent/timestamp),13 多进程需要时加文件锁(04 的「file locking arrives with multi-process needs」如期兑现),格式无需再变。

## 4. 树视图与 lane

### 4.1 树结构构建(`core/session/tree.py`,纯函数)

```python
def build_tree(entries: list[SessionEntry]) -> TreeView:
    """message 按 parent 链组织;lane 指针解析出命名分支;书签/摘要挂到 entry。"""
```

- 节点 = message entry(uuid 索引);`parent` 即树边(单亲,消息链天然是树,分支 = 不同根链)。
- 根:parent 为 None 的消息(文件里可能多条根 —— 分支起点);旧文件线性链 = 单根单链,树退化为 04 语义。
- lane 解析:按出现顺序遍历 lane entry,`{name: leaf_uuid}` 映射;**活跃 lane = 最后一条 lane entry**(= 该 lane 最新指针,§3.4 机制保证其 leaf 即最新消息);fork 的 lane entry(leaf=分支起点)在同一张映射表里,同名重复出现后者胜(校验指针推进)。
- 书签/摘要/操作按 `entry`/`leaf` 字段挂到目标节点(读端映射,不修改消息 entry)。

### 4.2 fork 语义(对齐 Pi session.ts:338-351)

```python
def fork(session: Session, entry_id: str, *, name: str | None = None) -> str:
    """从 entry_id 分支:追加新 lane entry,leaf = entry_id 本身(分支起点)。
    name 缺省 = "main-{n}"(n = 既有分支计数 + 1)。返回 lane name。"""
```

- **分支 = 追加一个 lane 指针**(对齐 Pi「分支/fork 是追加一个新的 lane 指针,天然可回滚」);`{scope: "branch"}` = 从指定 entryId 分支;整树复制(`{scope: "tree"}`)不做 —— 单文件已含全树,「复制整树」无意义(§4.4 线性视图选 lane 即「从任何先前位置继续」)。
- **fork 后继续写**:`append_lane` 已重置 Session 游标(活跃 lane=新名,parent 游标=entry_id),后续 `append_message` 的新消息 **parent = fork 点** —— 新分支从 entry_id 续写,完全绕过原分支后续消息(分支点=中间消息)或自然延续(分支点=leaf)。新消息后跟的 lane 指针用新名推进。
- **分支命名**:`main`(默认)、`main-1`、`main-2`…;`/fork <entryId> <name>` 可指定。
- **fork 不创建新文件、不复制消息**(对比 Kode 多文件方案):零拷贝,历史共享 —— 这就是「可共享的历史记录」。

### 4.3 线性视图(引擎消费面,04 兼容红线)

```python
def linear_messages(entries: list[SessionEntry], lane: str | None = None) -> list[SessionMessage]:
    """沿 lane 的 leaf 从根(无 parent)沿 parent 链走到 leaf,投影为 SessionMessage
    列表(丢弃应用状态 entry)。lane=None → 活跃 lane。leaf = 该 lane 最新指针指向的
    消息(§3.4 推进)或 fork 起点(未续写分支);分支共享前缀历史 —— 即「可共享的历史记录」。
    旧文件单 lane 行为与 04 load() 完全一致。"""
```

- `Session.load()` = `linear_messages(entries, active_lane)` —— **返回语义不变,引擎/REPL/压缩管线零改动**(loop.py:181 `self.history = config.history or []` 与 :254 `RunState(messages=[*self.history, first])` 的 history 装配、`_print_history_summary` 照旧)。
- 引擎每轮 append 只写消息 entry,引擎不感知 lane/树;树交互全部在 CLI 层(session 包 API + 斜杠命令)。

### 4.4 「从任何先前位置继续」的完整语义

- `--continue`(无参):沿活跃 lane(默认 main)继续 —— **07 语义完全保留**。
- `--continue --lane <name>`:沿指定 lane 的链继续(线性视图换 lane 即可,零新代码路径)。
- `/fork <entryId>` 后 `--continue`:新分支成为活跃 lane,后续消息挂新分支。
- `/tree <entryId>` 查看任意位置上下文后,`/fork <entryId>` 即「从那里继续」—— 导航与继续解耦,两步操作,语义清晰。

### 4.5 branch_summary 与恢复(10 裁决迁移 + 保留清单 #14)

- compaction 时(10 的 `_compact` 管线,loop.py:539-594):摘要文本经 `make_branch_summary_entry` 追加为 `branch_summary` entry(leaf = 压缩切点后的第一条消息 uuid,即「挂 leafUuid」),**不改动消息链,不删除被摘要覆盖的消息**(对齐 Pi「compaction 也是插入一个 entry」)。
- `--resume` 恢复:沿活跃 lane 找最近的 `branch_summary`,取其 `leaf` 指向的消息,往前保 **2 条 user 消息**(保留清单 #14)作为上下文起点;摘要文本注入 context(10 的 boundary 消息模式复用,`is_compaction_summary` 保位)。
- **跨 lane 过滤(成文)**:多分支文件按文件序找摘要会撞上**别的分支**的 `branch_summary` —— 候选摘要的 `leaf` 必须落在目标 lane 的链上(`leaf ∉ 链则跳过,继续往前找`);压缩发生时 `_compact` 在活跃 lane 上运行,其 leaf 天然属活跃 lane,不冲突。
- 08/10 的既有压缩行为零改动:压缩仍在内存态进行(boundary 消息在消息流里),branch_summary entry 只是**落盘快照**,供跨进程恢复用 —— 这正是 10-compact.md:50 裁决「SessionSummaryRecord(resume 稳定摘要)归阶段 12」的兑现。

## 5. fork/continue/resume 升级

| 入口 | 07 现状 | 12 升级 |
|---|---|---|
| `--continue` | 同文件重放 + 追加(cli/__init__.py:139-144,158-163) | 语义不变;追加 `--lane <name>` 选分支;启动时检测未完成操作(§7.3) |
| `--resume` | 摘要(最后 10 条)+ 新会话(cli/__init__.py:145-149,165) | 摘要改走 branch_summary(§4.5);新会话语义不变(仍是新文件 —— resume 是「总结后新开」,fork 才是「原地分支」,两者并存不混淆) |
| `--session-id` | 指定恢复(cli/__init__.py:134-138) | 语义不变,支持恢复任意 session 的活跃 lane |
| `/fork <entryId>` | 无 | **新增**:从任意 entry 分支(§4.2) |
| `/tree` | 无 | **新增**:树导航(§6) |

- **裁决:--resume 与 fork 的分工**。resume = 压缩/摘要后开新会话文件(轻量、省 context,07 既有语义);fork = 原地分支共享历史(树状导航的「继续」)。两语义并存,`--continue` 处于中间(同文件追加)。用户在 `--continue`(同文件)与 `--resume`(新文件)间的选择保持 07 不变,12 只加树维度。
- `--resume --lane <name>`:同样接受 lane 参数(在旧文件里选分支摘要)。

## 6. /tree 斜杠命令(用户重点)

注册到 `cli/commands.py`(CC-09 注册表,SlashCommand 数据对象):

```python
COMMANDS.append(SlashCommand("tree", _cmd_tree, "树状导航:渲染分支/书签,按类型筛选"))
COMMANDS.append(SlashCommand("fork", _cmd_fork, "从 entryId 分支: /fork <entryId> [name]"))
COMMANDS.append(SlashCommand("bookmark", _cmd_bookmark, "标记书签: /bookmark <entryId> <name>"))
```

**`/tree` 渲染**(对齐用户产品需求 + Pi 形态):

```
session 3f9a…  (7 entries, 2 branches)
main ───────────────────────────────────
  ✓ ① user  2026-08-12T10:00  "修复 auth 登录 bug"(★ auth-fix)
    ② user  2026-08-12T10:01  "再试一次,加日志"
    ③ assistant  …  "尝试了方案 A"
    ④ tool_use   …   Bash("npm test")
    ⑤ tool_result …   "2 failed"
    ⑥ assistant  …  "方案 A 失败,换 B"
    ⑦ assistant  …  "方案 B 通过"
main-1 ── fork @ ② ────────────────────
    ⑧ user  2026-08-12T10:15  "从 ② 继续,换方案 C"
    ⑨ assistant  …
```

- 每行:`书签 ✓` + 序号 + 类型(角色/工具块)+ 时间 + 内容截断(80 字符,ponytail 注释:超长截断阈值后续可配置)。
- 分支:`lane` entry 渲染为分支头,缩进对齐;活跃 lane 高亮(`→` 标记,文本模式用颜色码)。
- 筛选:`/tree --type user|assistant|tool_use|tool_result|bookmark|summary|operation`(用户需求「按消息类型筛选」;类型映射自 message 的 role/content block 类型 + 应用状态 entry 类型)。
- 只显示书签:`/tree --bookmarks`(用户需求「条目标记为书签」的查看面)。
- 页码:`/tree [n]` 翻页(每页 20 行,ponytail);`/tree <entryId>` 显示该 entry 所在分支的上下文窗口(前 5 后 3,含 parent 链标注)。

**`/bookmark <entryId> <name>`**:追加 bookmark entry;重名覆盖语义 = 追加新 entry(旧书签保留,读取时后者胜 —— 追加式不删除,对齐「永不删除」)。

**`/fork <entryId> [name]`**:§4.2;输出 `forked at <entryId> → lane <name>`。

## 7. 会话操作日志与中断恢复(PI-07)

### 7.1 记录什么(轻量单向)

引擎在工具执行处(`engine/loop.py` 工具轮,工具调用发起点)追加 `operation` entry:

```python
entry = session.append_operation(
    kind="tool_started",
    tool=tool_name,
    args_summary=str(tool_input)[:200],   # 截断,不进模型上下文(应用状态)
)
```

**裁剪裁决**:只记 `tool_started`(工具执行起点),不记配对 end(§1.2 已裁)。理由:中断恢复只需知道「最后一个工具在干什么」,配对事件要侵入引擎工具轮的全部出口(成功/失败/abort/钩子阻断),收益低、埋点面大;Kode 的完整 operation 配对留待后续强化(R6)。

### 7.2 读取:`find_open_operations`(纯函数)

```python
def find_open_operations(entries: list[SessionEntry]) -> list[SessionEntry]:
    """活跃 lane 上最后一段 operation:若最后一条 entry 是 operation(或其后只有
    应用状态 entry 而无可继续的消息),该操作视为「未完成」。"""
```

### 7.3 `--continue` 的恢复行为

- `--continue` 启动时调 `find_open_operations`;若存在未完成操作,打印提示:

```
Continuing session 3f9a… (12 messages)
[!] 上次运行中断于工具调用: Bash("npm test")(entry ④) —— 从该点继续
```

- 恢复语义:**提示 + 原样继续**(不自动重放操作 —— 工具副作用不可重放,重放是 13 子代理/编排的职责;12 只提供「中断点可见性」)。模型看到提示后自决策(对齐主规格 #2「工具失败转 tool_result 交模型自愈」的自愈精神)。
- `step_attempt` kind:预留(多步操作/子代理场景,13 用),12 只定义不埋点。

## 8. 会话自描述(PI-08)

### 8.1 meta entry(文件首行)

会话创建时写入首行:

```json
{"type": "meta", "uuid": "...", "timestamp": "...", "model": "main",
 "show_thinking": false, "cwd": "E:/Mac/CodeSage", "system_prompt_hash": "…",
 "session_id": "3f9a…"}
```

- 模型指针名(不解析字面量 —— 审计要的是「当时配置的指针」,字面量在 profile 里解析,pointer 名即配置身份)。
- 读端:`Session.meta` 属性;`--resume` 的摘要展示可标注原配置(「resuming 3f9a… (model main)」)。

### 8.2 model_change entry(会话内变更)

模型指针切换(`/model` 命令或引擎侧切换)时追加:

```json
{"type": "model_change", "from": "main", "to": "sonnet", "timestamp": "..."}
```

- 恢复/审计不用猜当时配置;树视图可筛选 `--type model_change` 查看变更历史。

### 8.3 标题/标签(CC-16 轻量采纳)

- 首条有意义 user prompt(非工具结果载体、非 reminder)提取为标题:由**追加的第二个 `meta` entry** 承载(`meta.title`,≤80 字符截断,ponytail)—— meta 首行已落盘,append-only 无法回改,标题 entry 追加在后、读取时后者胜(`Session.meta` 合并多个 meta entry)。
- 标签(`/tag <name>`):追加 `bookmark` entry 复用(bookmark 即「命名标记」,标签与书签同构,不新增 entry 类型)。
- `/sessions` 列表展示标题,会话选择体验提升。

## 9. 归档与会话选择器

### 9.1 归档(`core/session/archive.py`)

```python
def archive_session(root: Path, session_id: str) -> Path:
    """移动 sessions/{id}.jsonl → sessions/archive/{id}.jsonl(含 project_key 子目录);返回新路径。"""
def active_sessions(root: Path) -> list[SessionMeta]: ...   # 排除 archive/
def archived_sessions(root: Path) -> list[SessionMeta]: ...  # 仅 archive/
```

- **归档 = 移动文件**(原子性:同盘 rename,`os.replace`);归档目录:root 级 → `sessions/archive/`,project key 级 → `sessions/{project}/archive/`;**`list_sessions` 排除任何层级的 archive/ 目录**(§10.2 红线)。
- 归档不删除(可恢复:`/archive --restore <id>` 或手动移回;恢复命令实现成本一行,纳入)。
- `list_sessions`(core/session.py:62)改为排除 archive/ 目录 —— **语义微调红线**:04 测试断言文件枚举的用例需回归(归档后不可见)。

### 9.2 会话选择器支持(`/sessions`)

```
/sessions            # 活跃会话,按 mtime 倒序:id / 标题 / 消息数 / 分支数 / 时间
/sessions --archive  # 归档会话
/sessions --all      # 两者
```

- 与 07 `--session-id` 组合:从列表里挑 id → `--session-id <id> --continue`。
- 交互式选择菜单:**裁剪**(§1.2),列表 + 参数组合已覆盖脚本/手动两用。

## 10. 测试计划

### 10.1 镜像清单(`tests/…`,镜像实现文件)

| 测试文件 | 镜像 | 用例要点 |
|---|---|---|
| `tests/core/test_entry.py` **新增** | `session/entry.py` | 七类 entry 序列化 roundtrip;无 type 键旧行 → message 兼容;损坏行跳过;parent 缺省推导 |
| `tests/core/test_tree.py` **新增** | `session/tree.py` | 单链树退化;分支渲染(多根 + lane 解析);类型筛选;书签挂载;活跃 lane 判定 |
| `tests/core/test_fork.py` **新增** | `session/session.py` + tree | fork 追加 lane;fork 后写消息挂新分支;线性视图随活跃 lane 变化;命名 main-1 递增;单文件断言(分支后文件数不变) |
| `tests/core/test_operations.py` **新增** | `session/session.py` + tree | operation 追加;find_open_operations 命中(末尾 operation)与不命中(末尾是消息);中断点标注 |
| `tests/core/test_archive.py` **新增** | `session/archive.py` | 归档移动(project 子目录);active/archived 枚举;restore;list_sessions 排除 archive |
| `tests/core/test_session.py` **既有,零改动** | 04 | 旧格式 roundtrip/append/load/损坏行 —— **04 契约回归红线** |
| `tests/cli/test_commands_tree.py` **新增** | `cli/commands.py` | /tree 渲染含分支;--type 筛选;--bookmarks;/fork 输出;/bookmark 追加 |
| `tests/cli/test_resume_inject.py` **新增** | `cli/__init__.py` resume 区块 | --resume 注入 branch_summary 摘要 + leaf 链前 2 条 user 为上下文起点;跨 lane 过滤(多分支文件选对分支摘要);07 resume 行为回归(无摘要时 = 旧逻辑) |
| `tests/engine/test_loop.py` **追加** | 装配 | E2E:会话文件首行 meta;engine append 产生 message entry 带 parent 链;compact 落 branch_summary entry;model_change entry(装配时注入) |

### 10.2 不能破坏的既有契约(12 改动红线)

| 红线 | 锚点 | 说明 |
|---|---|---|
| Session 构造签名与 append/load 返回语义 | `core/session.py`、`tests/core/test_session.py` | `Session(session_id, root, project_key)` 签名不变;`load()` 返回线性 SessionMessage 列表(活跃 lane);旧文件读取行为逐字节一致 |
| 04 序列化契约 | `core/messages.py::to_dict/from_dict` | SessionMessage 零改动;消息 entry 复用其序列化 |
| engine/REPL/压缩的 session 消费 | `engine/loop.py`(history 装配)、`cli/__init__.py` | 只吃线性视图,零改动(§4.3) |
| resume 三命令既有语义 | `cli/__init__.py:72-76,128-165` | --continue/--resume/--session-id 行为不变(12 只加 --lane 与提示) |
| 斜杠命令注册表 | `cli/commands.py` | 只追加不修改既有命令;HELP_TEXT 自动生成 |
| 10 压缩管线 | `engine/loop.py:539-594`、`compaction.py` | 内存态压缩零改动;branch_summary 落盘是**附加写入**,不改变 boundary 消息流 |
| audit/hooks/权限 | `permissions/`、`hooks/` | 零改动(会话格式不涉权限面) |

### 10.3 回归

`python -m pytest tests/ -q` 全绿(基线 918,2026-08-08;新增后增长);04 的 `test_session.py` 原样通过。

## 11. 实施步骤

| 步 | 内容 | 文件:锚点 | 闸门 |
|---|---|---|---|
| S1 | entry 模型 + 序列化 + 旧格式兼容 | `core/session/entry.py`、`session.py` 迁移改造(append 变 append_message,load 兼容双格式) | `test_entry.py` 绿 + `test_session.py`(04)零改动通过 |
| S2 | 树视图 + lane 解析 + fork + bookmark | `core/session/tree.py`;`session.py::fork/append_bookmark` | `test_tree.py` + `test_fork.py` 绿 |
| S3 | 操作日志 + meta/model_change + branch_summary | `session.py::append_operation/append_meta`;`find_open_operations`;loop 埋点(工具轮 + 装配 meta + compact 落盘) | `test_operations.py` 绿;loop E2E 断言 entry 形状 |
| S4 | CLI 入口升级:`--lane` 参数 + `--resume` 摘要注入(branch_summary → leaf 链上前 2 条 user 为上下文起点,§4.5 跨 lane 过滤)+ `--continue` 中断恢复提示 | `cli/__init__.py`(resume 区块改造);`cli/assemble.py` lane 装配 | **resume 改造新测试**(摘要注入 + 跨 lane 过滤)+ 07 resume 既有测试回归 |
| S5 | 斜杠命令 + 归档:`/tree` `/fork` `/bookmark` `/sessions` + `/archive`(`--restore`) | `cli/commands.py` 追加;`session/archive.py` | `test_commands_tree.py` + `test_archive.py` 绿 |
| S6 | 红线固化 + 理解文档 | `docs/modules/12-session.md` | 全量回归绿(918 + 新增);04 会话测试零改动 |

依赖:S1 → S2 → S3 → S4 → S5;S6 收尾。步骤间每步独立提交。依赖外部:04(消息/会话契约)、07(cli resume/commands)、10(压缩摘要管线)—— 全部已就绪,零新依赖(Ask first 不触发)。

## 12. 风险与边界

| # | 风险 | 缓解 |
|---|---|---|
| R1 | **04 存储格式变更红线**(04 spec「Ask first: 改变会话存储格式」) | 本 spec 即 Ask(用户批准即放行);兼容策略(§3.3 旧行惰性推导)保证旧文件读取行为逐字节一致;`test_session.py` 零改动是硬闸门;核心不变量(append-only/fsync/损坏容错)原样保留 |
| R2 | **树状复杂度失控**(全功能树 UI/编辑器) | §1.2 裁剪:只做渲染 + 筛选 + 书签 + fork 四件套;无树编辑(移动/删除节点)、无交互式选择器;每新增需求先过裁剪表 |
| R3 | load() 语义漂移(引擎消费面破坏) | §4.3 单一出口:`load()` ≡ `linear_messages(entries, active_lane)`;loop 的 history 装配处加 E2E 断言(§10.1) |
| R4 | lane 指针损坏(坏行恰为最后一条 lane) | 读端容错:lane 解析失败 → 退回上一个合法 lane;全失败 → 单 lane main 兜底(§3.4) |
| R5 | branch_summary 与内存压缩的双份摘要漂移;多分支文件里摘要跨 lane 误用 | 裁决:branch_summary 是落盘快照(读端展示用),内存压缩仍是唯一权威(boundary 消息流不变);恢复时 branch_summary 只作上下文起点,且 **leaf ∉ 目标 lane 链则跳过**(§4.5 跨 lane 过滤) |
| R6 | 操作日志单向(无配对 end)导致「假未完成」 | 检测启发式(§7.2:末尾是 operation 即视为未完成)可能误报 —— 误报只产生提示,不自动重放(§7.3),无副作用;配对事件留待 13 子代理场景强化 |
| R7 | fork 后 `--continue` 语义混淆(用户预期) | 文档化:--continue 默认沿活跃 lane(= 07 语义);--lane 显式选分支;`/tree` 可视化分支状态,提示语含 lane 名 |
| R8 | 归档移动破坏会话 id 枚举(04 list_sessions 语义) | §9.1 显式红线:list_sessions 排除 archive/,04 相关用例回归;归档路径可逆(--restore) |
| R9 | 文件体积(单文件含全分支历史 + 摘要快照) | 追加式单文件是用户指定(「所有分支保存在单个文件中」);体积由压缩(branch_summary 快照)与归档(§9)共同缓解;树渲染按页截断(§6) |
| R10 | 多进程并发写(13 场景) | §3.5 格式预留(每行自包含);12 保持单写者,不实现锁(04 的兑现点在 13) |

## 13. 与路线图的关系

- **04 → 12**:04 spec「不做:fork/resume/归档(阶段 12)」如期兑现;04 契约红线(R1)由 §3.3 兼容策略守护;`core/session.py` 原地升级为包(§3.1),`core/__init__.py` 导出面不变。
- **10 → 12**:10-compact.md:50「SessionSummaryRecord(resume 稳定摘要)归阶段 12」→ §4.5 branch_summary entry;10 的摘要管线零改动,只加落盘快照写入点。
- **12 → 13(子代理)**:fork API(§4.2)成为 13 forkContext 的存储基座(13 传 `session_id + lane name` 即完全定位历史);`step_attempt` operation kind 预留(13 埋点);操作日志配对的完整语义 13 再评(R6);resume 从转录缓存恢复(保留清单 #15)归 13。
- **主规格留痕(修订)**:codesage.md 路线图 153 行「12 session 会话生命周期:fork/continue/resume、sidechain 日志、归档、会话选择器支持」—— 全部落位(fork/continue/resume §5、sidechain 日志 = 操作日志 §7 + 树状分支 §4、归档 §9、会话选择器 §9.2);并补「树状会话(单文件多分支 + /tree + 书签 + 类型筛选)」于该行(用户新增需求,PI-09);保留清单 #14「summary 挂 leafUuid,恢复保摘要前 2 条 user 消息」由 §4.5 兑现。
- **11 → 12**:任务存储独立,会话文件形状变化零影响;fork 分支共享 session_id → 共享 taskListId(11-tasks.md §12:514 行已裁),12 不改任务侧。
- **12 → 17(记忆)**:meta entry(§8)的会话自描述是 17 记忆提取的「可信锚点」(当时配置),17 落地时复用读端。
- **与 CC-16 的关系**:标题提取(§8.3)轻量采纳,粘贴引用化裁剪(§1.2)。

---

*附:探索依据(2026-08-12)探索确认 07 已交付 --continue/--resume/--session-id(cli/__init__.py:72-76,128-165,resume 摘要 = 最后 10 条渲染)+ CC-09 斜杠命令注册表(cli/commands.py,SlashCommand 数据对象 + find_command,12 的五个新命令零范式直接注册);10 压缩管线(generate_summary/UPDATE 迭代/boundary 消息)已就绪,SessionSummaryRecord 裁决在 10-compact.md:50 明确「归阶段 12」;docs/pi-agent-core-analysis.md:102-105 的 PI 表把 PI-07/08 归 core/session、PI-09 标「阶段 12 升级」、PI-10 标「阶段 12/19」;pi-agent-core-analysis.md §2 记录了用户树状产品需求原文(「/tree 导航至任何先前位置并从那里继续;所有分支保存在单个文件中;可按消息类型筛选;条目标记为书签」)+ Pi 的 entry 链/lane 指针设计(types.ts:14-74/150-212, fork scope branch/tree, session.ts:338-351);Kode 对照(packages/core/src/logging/log/paths.ts)确认其 sidechain 为多文件命名分支,因用户指定单文件而否决;04 spec「不做:fork/resume/归档(阶段 12)」确认边界归属。*
