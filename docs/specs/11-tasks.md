# 阶段 11:tasks 任务系统

> 基于:阶段 03 工具契约与 TodoWrite 种子(已实现)+ 阶段 04 会话存储(已实现)+ 阶段 09 钩子系统(已实现)+ todo.md 11 条目 + Claude Code TodoV2 / Kode-CLI 任务系统探索(w1-ref-doc 报告 `docs/reference/task-system.md`)+ codesage 现状扫描(w2)+ spec 格式与边界研究(w3)。
> 前置规格:`docs/specs/codesage.md`(主规格,198 行 Open Question 于 §2 裁决)、`docs/specs/03-tools.md`、`docs/specs/04-core.md`、`docs/specs/10-compact.md`(§1.2 裁决复核见 §12)。
> 参考实现(只读):Kode-CLI `packages/core/src/tasks/{types,storage}.ts`、`packages/core/src/automation/taskGraph.ts`、`packages/tools/src/tools/interaction/Task*Tool`。

## 0. 验收标准(tasks/todo.md 39-41 行)

- [ ] Task CRUD 全链路:`TaskCreate`/`TaskGet`/`TaskList`/`TaskUpdate`(含 `status="deleted"` 删除)四工具注册并可用(`tools/builtin/__init__.py:16` BUILTIN_TOOLS + `tools/builtin/interaction/task_*.py`),任务持久化到 `{config_dir}/tasks/{task_list_id}/`(`core/tasks/storage.py`)
- [ ] blocks/blockedBy 双向依赖 + 环检测:**mutation 时预防**(自环拒绝、加边前 DFS 路径检查拒绝成环,`core/tasks/storage.py::_has_path`)+ **读取时全量验证**(四类:`duplicate_task_ids`/`missing_dependencies`/`cycles`/`asymmetric_dependencies`,`core/tasks/graph.py::validate_task_graph`)
- [ ] todo 衔接:TodoWrite(03 交付,内存版)契约零破坏 —— 并存裁决成文(§7),`tests/tools/test_todo.py` 全部原样保留并回归通过
- [ ] 命名避让:与 `TaskOutput`/`TaskStop`(后台 Bash,`tools/builtin/system/task.py`)共存,新模块命名裁决成文(§7.3)
- [ ] 端到端:mock LLM 单轮 run 中模型经 TaskCreate → TaskList → TaskUpdate 完成多步任务拆解/认领/完成(对齐 07-cli.md V1「真实 API 端到端完成任务」验收精神与 10 的 boundary 固化,`tests/engine/test_loop.py` 追加)
- [ ] 验证:依赖图单测(环/缺失/重复/非对称,`tests/core/tasks/test_graph.py`)+ 存储单测(`test_storage.py`)+ 工具单测(4 文件)+ 全量回归 `python -m pytest tests/ -q` 全绿(基线 838,2026-08-08 collect-only 实测;新增用例后增长)

## 1. 目标与范围

### 1.1 做什么

阶段 11 把 03 交付的「内存 TodoWrite 种子」升级为 **TodoV2 式持久化任务系统**:一任务一 JSON 文件 + 双粒度锁 + 高水位 ID + 双向依赖 + 环检测。核心需求来自 todo.md 11 条目与主规格路线图(152 行):

| 需求 | 原文(todo.md / 主规格) | 落位 |
|---|---|---|
| Task CRUD | 「Task CRUD」 | §6 四工具 + §3 存储 |
| 依赖环检测 | 「blocks/blockedBy 环检测」 | §5 |
| todo | 「todo」 | §7 TodoWrite 衔接 |
| 持久化层级 | 主规格 198 行 Open Question「任务持久化层级——阶段 11 衔接时定」 | §2 裁决 |
| 子代理前置 | 主规格 154 行「阶段 13:…Task 工具」依赖 11 的存储与工具 | §12 |

具体交付(编号,§10 步骤映射):

1. **`core/tasks/` 包**(契约层 `types.py` + 实现层 `storage.py` + 只读图视图 `graph.py`):任务模型、一任务一文件存储、自增 ID + 高水位、O_EXCL 文件锁 + asyncio 进程内互斥、双向依赖维护、环预防与全量验证。
2. **四工具**:`TaskCreate`/`TaskGet`/`TaskList`/`TaskUpdate`(`tools/builtin/interaction/task_create.py` 等 4 文件,文件名即工具名,对齐 `ls.py`/`read.py` 规范),`needs_permissions()=False` + `SYSTEM_TOOLS` 白名单。
3. **装配接线**:`ToolUseContext.task_list_id` 字段(引擎 loop.py:744 注入 session id)、`cli/assemble.py` 传递;`modes.py:26` SYSTEM_TOOLS 追加。
4. **并发语义**:进程内 asyncio.Lock 单飞 mutation + 跨进程目录级文件锁(§4),为阶段 13 多代理并发铺底。
5. **测试 + 理解文档**:`tests/core/tasks/` + `tests/tools/test_task_{create,get,list,update}.py` + `docs/modules/11-tasks.md`。

### 1.2 不做什么(候选裁剪裁决表)

| 候选(探索发现) | 来源 | 裁决 |
|---|---|---|
| Task 工具归阶段 13 | 主规格 154 行「13 子代理:…Task 工具」 | ❌ **工具属 11**。13 只加多代理扩展(自动 owner、claimTaskWithBusyCheck、Mailbox、unassignTeammateTasks),工具本体与存储必须在 11 交付 —— 主规格把「Task 工具」列在 13 是因其多代理语义,存储与单代理工具属 11 路线图行(152)「任务系统:Task CRUD、依赖环检测、todo」 |
| TodoWrite 持久化/替换 | w2 扫描:TodoWrite 内存版(注释「per-session key lands with phase 06」,但 06 未兑现,store 仍硬编码 "default" —— 此事实反而强化并存裁决) | ❌ **并存**(§7)。TodoWrite 保持内存原样:03 契约(单列表幂等替换、单 in_progress 不变量、`needs_permissions=False`)有测试锁定;Kode 自身也是 TodoWrite 与 Task 系列并存;合并收益远小于重写 03 契约成本 |
| UI 三层变更检测(fs.watch/信号/轮询)+ Singleton Store | docs/reference/task-system.md §11.3 | ❌ UI 是 REPL 渲染层的事,归阶段 12(会话生命周期 UX,与 10-compact §1.2「UX 交互归阶段 12」同口径)。11 只做存储 + 工具 |
| 周期性任务提醒注入(isMeta system-reminder,10 轮) | task-system.md §11.4 | ❌ 不纳入 11。触发条件状态机(距上次写/距上次提醒各 ≥10 轮)属上下文工程;13 子代理场景再评(§12 复核)。注入通道(is_reminder,08 已建)现成,后续零新范式 |
| TaskCreated/TaskCompleted Hooks | task-system.md §11.7;09-hooks.md:34 已将 `Task*` 列为扩展来源「依赖阶段 13/11(未建)」 | ❌ **触发与消费者留 13**(§12 衔接裁决)。09 八事件 v1 不变:任务 hook 是外部同步/合规场景,11 单代理无消费者;09 扩展机制(加事件不破坏既有)保证 13 需要时成本低;11 只保证 storage 层 mutation 单点,13 加事件 emit 零重构 |
| `_internal` 元数据隐藏 | task-system.md §11.1 | ❌ 内部任务隐藏是 UI 层需求,11 无 UI 消费者。metadata 仍是任意 dict(13 用) |
| `.tombstones.json` 墓碑 | Kode storage.ts:25 | ❌ Kode 墓碑防「多 data root 下 legacy 任务复活」;CodeSage 单一 store root,删除 = 真删文件 + 清理引用 + 更新高水位,无复活路径(§3.4) |
| claimTaskWithBusyCheck 原子认领 | task-system.md §11.5 | ❌ agent swarms 语义,13 交付(§12);11 只保证存储层并发正确(§4) |
| 任务级细锁(per-task .lock) | task-system.md §11.2 | ❌ 单锁裁决见 §4.2(吞吐未到,任务级锁是预留,不实现) |
| 验证提醒(Verification Nudge) | task-system.md §11.6 | ❌ feature-flag 实验特性,13 之后评 |
| `getReadyTasks`/`getCriticalTaskBlockers` 调度视图 | Kode taskGraph.ts:334-429 | ❌ supervisor 调度语义属 13;11 只交付 `validate_task_graph`(todo 验收「环/缺失」仅需验证) |
| 系统提示词任务引导(todo 的 11.8) | task-system.md §11.8 | ❌ base_prompt 属 08 上下文组装;11 交付工具后 13 接系统提示词引导 |

### 1.3 三分法边界

- **已有,11 复用**:`config/atomic.py:16 atomic_write`(任务文件/高水位原子写,tmp+rename+fsync,主规格 #14);`config/atomic.py:58 read_json_lossy` 的「损坏降级」语义(任务文件损坏跳过不致命,主规格 #2);`config/paths.py:25 config_dir()`(数据根);`permissions/paths.py:17` 写保护(`.codesage` 已在 WRITE_PROTECTED_COMPONENTS —— 任务目录天然不可被 Write/Edit 直写,零新增防护);`permissions/modes.py:26` SYSTEM_TOOLS 白名单先例(TodoWrite);`tools/base.py` Tool 契约(needs_permissions/validate_input/async generator);`engine/tool_queue.py:127` 并发屏障;`core/session.py:18` `_SANITIZE_PROJECT` 正则(同款 sanitize taskListId);`cli/assemble.py:61` session_id 装配点。
- **11 新增**:`core/tasks/` 包(types/storage/graph);四工具文件;SYSTEM_TOOLS 追加 4 名;`ToolUseContext.task_list_id` 字段;`engine/loop.py:744` 注入。
- **语义微调**(红线,需回归):`tools/base.py:33` ToolUseContext **加字段** `task_list_id: str = "default"`(dataclass slots,带默认值,既有构造点零破坏);`modes.py:26` SYSTEM_TOOLS **只增不删**。

## 2. 持久化层级裁决(主规格 198 行 Open Question 落定)

**裁决:任务用独立项目级存储,不挂会话 —— `{config_dir}/tasks/{task_list_id}/`,其中 config_dir = `paths.config_dir()`(`~/.codesage` 或 `CODESAGE_CONFIG_DIR`)。**

理由(三层生命周期视角):

1. **数据语义不兼容**(否决「挂会话」的直接理由):04 会话是 append-only 不可变 JSONL(`core/session.py:5-6` 自述「single-writer assumption:file locking arrives with multi-process needs」),任务是**可变状态读-改-写 CRUD**(状态/owner/依赖变更),两者写入模型根本不同 —— 任务塞进会话文件 = 用 append 模拟 update,重写 04 契约。
2. **12 fork/resume 不破坏**:会话 fork 后是另一条消息流,任务必须跨会话可见(12 语义:任务在 fork 出的新会话里继续)。任务挂会话 = fork 时任务断链。
3. **13 多代理共享**:teammate 靠 taskListId(team name)共享同一任务目录,任务必须独立于任一单一会话文件。
4. **存储位置与 config 目录的关系**:放 `config_dir()` 下而非 cwd,因为(a) 与 sessions/audit/memory 同级,都是 harness 状态(config 目录既定职责);(b) 多项目共享团队任务需要跨 cwd;(c) `.codesage` 组件已在写保护清单,模型无法绕过工具直写 —— 安全地板免费获得。任务列表隔离靠 taskListId 分层(单会话→session id;团队→team name;外部→env),与 Kode `~/.claude/tasks/{taskListId}/` 同构。

**被拒方案**:

| 方案 | 拒绝理由 |
|---|---|
| 挂会话目录(`sessions/{session_id}/tasks/`) | 12 fork 断链;13 无法跨会话共享;JSONL 无锁 update 语义 |
| cwd 下 `.codesage/tasks/`(项目级) | 团队任务跨 cwd 失效;可能被 git 提交污染;与既有 sessions 同级惯例不符 |
| 数据库(sqlite 等) | 违反主规格 #14「无数据库」;一任务一文件的锁粒度优势全失 |

**对 12 的影响:无。** 任务跨会话可见(同一 session id 的 fork 链共享 taskListId),12 会话生命周期不感知任务;12 需处理的仅是「任务文件生命周期长于会话」这一事实,与 12 无交互。

## 3. 存储设计

### 3.1 目录布局

```
{config_dir}/tasks/{task_list_id}/
  .lock              # 目录级锁文件(O_EXCL 原子创建,内容 "pid timestamp")
  .highwatermark     # 历史最高任务 ID(防删除后 ID 重用)
  1.json             # 任务 1(一任务一文件)
  2.json             # 任务 2
```

- `task_list_id` sanitize:`[^A-Za-z0-9_-]+` → `-`(镜像 Kode `sanitizeTaskListId`,与 `core/session.py:18` `_SANITIZE_PROJECT` 同款正则,新正则放 `core/tasks/storage.py` 私有)。
- 任务文件名为纯整数 ID,无需 sanitize。

### 3.2 文件格式(一任务一 JSON,对齐 Kode types.ts)

```json
{
  "id": "3",
  "subject": "Fix authentication bug",
  "description": "The login endpoint returns 500 on empty session cookie...",
  "activeForm": "Fixing authentication bug",
  "status": "in_progress",
  "owner": null,
  "blocks": ["1", "2"],
  "blockedBy": [],
  "metadata": {}
}
```

**字段命名裁决(成文)**:落盘 JSON 用 **snake_case**(`active_form`/`blocked_by`),对齐代码库 pydantic 全 snake_case 惯例(core/session.py、ai/types.py)与 §5.3 的 dataclass 草图;上方样例的 camelCase 是 Kode types.ts 对照,非 CodeSage 格式。python 侧 pydantic 字段名即 JSON 键(不启用 alias),S2 存储直接 dump,零转换。

**裁决:一任务一文件(对齐 TodoV2/Kode),否决单文件列表。** 理由:锁粒度从「整个列表」细化到「单个任务」是多代理并发的基础(TodoV1→V2 的关键演进);CodeSage 03 的 TodoWrite 正是单列表形态,已被 TodoV1 证伪为不可并发,任务系统不复刻它。阶段 11 虽无多进程写,但存储格式按并发设计,13 不改格式直接加锁语义。

### 3.3 ID 分配:自增整数 + `.highwatermark`

**裁决:自增整数 ID,否决 UUID。** 理由:可读性(对话里「#3」可引用)、按序处理语义(早期任务为后续铺上下文)、文件排序即任务排序。

ID 分配(镜像 Kode `getNextTaskId`,目录锁内执行):

```python
def _next_id(dir: Path) -> str:
    """目录锁内调用:max(现有任务文件最大 ID, 高水位) + 1,写回高水位。"""
    files_max = max((int(p.stem) for p in dir.glob("*.json")
                     if p.stem.isdigit()), default=0)
    mark = _read_highwatermark(dir)
    next_id = max(files_max, mark) + 1
    _write_highwatermark(dir, next_id)
    return str(next_id)
```

- **删除时更新高水位**(`max(id, 当前高水位)` 写入):即使所有任务文件被删,历史最大 ID 仍记录,新任务不重用旧 ID —— 对话中已引用的「#3」不产生歧义。
- 高水位写入失败降级为 best-effort(不致命,下次创建时文件扫描兜底)。

### 3.4 删除语义:真删 + 引用清理,否决墓碑

**裁决:删除 = 目录锁内(1) 更新高水位;(2) 删任务文件;(3) 遍历其余任务,清除对它的 blocks/blockedBy 引用(引用清理是 best-effort,单任务写原子)。** 工具面入口:`TaskUpdate({taskId, status: "deleted"})`(镜像 Kode TaskUpdateTool)。

`deleted` 不是状态:状态机仍是 pending/in_progress/completed 三态,`deleted` 是工具面动作。被删任务被其他任务引用 → 引用在删除时清除;若手工编辑文件造成悬空引用(缺另一端),由 `graph.py` 的 `missing_dependencies` 在读取时诊断(§5.3)——「存储不变量靠 mutation 预防,外部编辑靠读取诊断」双保险。

**否决 `.tombstones.json`**:Kode 的墓碑防「多 data root 中 legacy 同名任务复活」(storage.ts:25,跨 store 合并读取场景);CodeSage 单一 store root,删文件后无任何路径复活同名 ID(高水位已挡),墓碑是死代码。

## 4. 锁与并发

### 4.1 现状与问题

- 引擎单进程单事件循环;concurrency-safe 工具经 `asyncio.gather` 交错执行(`engine/tool_queue.py:127`)。任务 mutation 是读-改-写,两个工具在 await 点交错会撕裂读改写。
- 同步阻塞锁(如线程互斥量、同步 sleep 重试)在事件循环内会卡死 loop。
- 项目 Windows 优先开发(`fcntl` 仅 POSIX,`msvcrt.locking` 仅 Windows)——需要跨平台方案,且「不新增依赖」(主规格 26 行依赖纪律)。

### 4.2 裁决:双层锁,进程内 asyncio.Lock + 跨进程 O_EXCL 文件锁,单粒度(目录级)

```python
class TaskStore:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root or paths.config_dir() / "tasks"
        self._in_proc = asyncio.Lock()      # 进程内:事件循环内互斥,mutation 全部过它

    async def create(self, ...) -> Task:
        async with self._in_proc:           # 进程内单飞
            with _dir_lock(self._dir(task_list_id)):   # 跨进程互斥
                ...  # 全部文件操作同步执行,锁内无 await
```

**跨进程文件锁(零依赖,镜像 Kode `acquireFileLock`)**:`os.open(lock_path, O_CREAT|O_EXCL|O_WRONLY)` 原子创建锁文件 —— 成功即持锁,`FileExistsError` 即被占用;`open(..., "x")` 在 Python 中跨平台映射 `_O_EXCL`(Windows 上语义可靠,测试固化)。失败路径:检查锁文件 mtime 超过 `STALE_S = 10s` → 视为死锁(进程崩溃残留)unlink 重试;重试 `30` 次、间隔 `50ms`(总等待 ≈ 1.5s,镜像 Kode LOCK_OPTIONS 的量级);超限抛 `TaskStoreError`(工具层转 is_error 返回给模型自愈)。

```python
@contextmanager
def _dir_lock(dir: Path, *, retries: int = 30, delay_s: float = 0.05, stale_s: float = 10.0):
    """O_EXCL 原子创建 + stale 回收 + 指数重试。退出时 unlink。"""
    lock_path = dir / ".lock"
    for attempt in range(retries):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
            os.close(fd)
            break
        except (FileExistsError, PermissionError):
            # PermissionError: Windows AV 瞬时占用,与原子写先例(config/atomic.py:41-46)同构
            try:
                if time.time() - lock_path.stat().st_mtime > stale_s:
                    lock_path.unlink()
            except OSError:
                pass
            time.sleep(delay_s)
    else:
        raise TaskStoreError("Failed to acquire task store lock")
    try:
        yield
    finally:
        # 仅删自己持有的锁:stale 误判时后继者已建新锁,删它会放行双写者
        try:
            owner = lock_path.read_text().split()[0]
            if owner == str(os.getpid()):
                lock_path.unlink()
        except OSError:
            pass
```

**双粒度裁决:11 只实现目录级单锁,任务级锁是预留不实现(ponytail 注释标注)。** 理由:任务级锁的价值是「10+ 并发 swarm agent 写不同任务互不阻塞」的吞吐优化;CodeSage 阶段 11 场景是单代理 + 少量工具调用,任务写操作本身是 µs-ms 级小文件原子写,目录级串行完全够用;而 `updateTaskWithDependencies` 的双向依赖更新、`create` 的 ID 分配、`delete` 的引用清理**本质就需要目录级锁**(跨任务原子性)——单锁方案天然避免「任务级 + 目录级嵌套锁」的死锁顺序问题。13 吞吐成为瓶颈时再加任务级锁,存储层 API 已按此预留(每个任务文件独立读改写,mutation 只要求目录锁,任务级锁是对既有调用点的优化而非重构)。

**进程内为什么也要锁**:虽然锁内无 await 点、同步 I/O 在单 loop 内天然原子,但工具 `_run` 的外层编排(如 `TaskUpdate` 需要先 `get` 再 `update`,中间有 await)可能在事件循环内交错 —— 一行 `asyncio.Lock` 消除这个脆弱假设,且为将来「锁内走 asyncio.to_thread」留正确性底线。**读不锁**(原子写保证读者看到完整旧/新文件,读端容忍瞬时不一致)。

**update 的读-改-写契约(显式)**:`update` 必须在目录锁内**重读**目标任务文件做完整读-改-写(锁外 `get` 仅用于存在性预检与友好报错,其结果不得作为 patch 基础)——否则并发双 update 会丢失更新(锁外读的旧值覆盖锁内新值)。

**同步 sleep 卡 loop 的风险**:`_dir_lock` 的 `time.sleep` 在事件循环内阻塞 —— 仅发生在跨进程锁冲突时(阶段 11 单进程下实际上不可达);重试总等待 1.5s 上限,写进 R2 缓解:13 引入真实跨进程前,把锁获取挪进 `asyncio.to_thread` 即可,API 不变。

## 5. 依赖与环检测

### 5.1 双向维护(对齐 Kode `blockTask`/TodoV2)

`blocks` 与 `blockedBy` 两处各存(免遍历):`TaskUpdate({taskId: 2, addBlockedBy: ["1"]})` 底层 = 写任务 2 的 `blockedBy += ["1"]` **且**写任务 1 的 `blocks += ["2"]`。理由:`TaskList` 判断可认领看 `blockedBy` 是否为空;`TaskGet` 展示下游看 `blocks` —— 各存一份,查询 O(1) 免全表遍历。

```python
# core/tasks/storage.py —— addEdge(source_id, target_id):source blocks target
def _add_edge(tasks: dict[str, Task], source: Task, target: Task) -> None:
    if target.id not in source.blocks:      # 幂等:重复声明不重复写
        source.blocks.append(target.id)
    if source.id not in target.blockedBy:
        target.blockedBy.append(source.id)
```

### 5.2 Mutation 时校验(预防:存储不变量)

`update` 加边前(目录锁内)依序检查:

1. **自环拒绝**:`add_blocks`/`add_blocked_by` 含任务自身 → 错误「Task #N cannot depend on itself」(镜像 Kode storage.ts:606-616)。
2. **目标存在性**:依赖的 id 无对应任务文件 → 错误「Task not found: N」。
3. **成环拒绝**(新增边前 DFS):若 `source → target` 已存在路径 `target ⇝ source`(沿 blocks 遍历),则加边成环 → 错误「Adding dependency S -> T would create a cycle」。

```python
def _has_path(tasks_by_id: dict[str, Task], from_id: str, to_id: str) -> bool:
    """沿 blocks 边从 from_id 出发能否到达 to_id(DFS,防加边成环)。"""
    stack, visited = [from_id], set()
    while stack:
        cur = stack.pop()
        if cur == to_id:
            return True
        if cur in visited:
            continue
        visited.add(cur)
        task = tasks_by_id.get(cur)
        if task:
            stack.extend(b for b in task.blocks if b not in visited)
    return False
```

**裁决:mutation 校验 + 读取校验都做,职责不同。** mutation 校验防坏数据**进入**存储(模型/工具是唯一合法写入者,闭环内环不可能产生);读取校验诊断存储里被**外部编辑**污染的数据(用户手工改任务文件、旧版本工具残留)。Kode 的 taskGraph 只读视图在 mutation 时校验的落点即此双轨。

### 5.3 读取时全量验证(诊断):`core/tasks/graph.py`

纯函数模块,镜像 Kode `taskGraph.ts` 的四类验证(不含其调度视图,§1.2 已裁):

```python
# core/tasks/graph.py
@dataclass(slots=True)
class MissingTaskDependency:
    task_id: str
    dependency_id: str
    declaration: str          # "blocks" | "blockedBy"

@dataclass(slots=True)
class TaskGraphValidation:
    valid: bool
    duplicate_task_ids: list[str]                 # 同 ID 出现多次
    missing_dependencies: list[MissingTaskDependency]   # 引用不存在的任务
    cycles: list[list[str]]                       # 每环重复首成员于尾,便于渲染(镜像 Kode normalizeCycle)
    asymmetric_dependencies: list[tuple[str, str, list[str]]]  # 仅单端声明(非致命)

def validate_task_graph(tasks: list[Task]) -> TaskGraphValidation:
    """纯函数、无 IO、确定性 —— 测试与 13 的 supervisor 共用。"""
```

实现要点(镜像 Kode createGraphIndex):edges 从 `blocks` + `blockedBy` 双声明归一(同一条边两个声明合并去重);id trim 去重(`uniqueIds`);环检测 DFS 三色标记(`visiting`/`visited`)并规范化环起点;`valid = 前三类全空`(非对称仅诊断不判无效)。

**性能**:O(V+E),任务数百规模 µs 级,无热点;mutation 时只跑 `_has_path`(单条路径 DFS),不做全量验证。

## 6. 四工具设计

### 6.1 契约总表

| 工具 | 职责 | is_concurrency_safe | needs_permissions | 输出 |
|---|---|---|---|---|
| TaskCreate | 创建任务 | False(写 store) | False | `Created task #3: <subject>` |
| TaskGet | 单任务详情 | True(只读) | False | 单行 JSON(全字段,模型无损解析) |
| TaskList | 任务摘要列表 | True(只读) | False | 文本行 `#3 [pending] Fix auth (alice) [blocked by #1, #2]`,按 ID 升序;空 → `No tasks found` |
| TaskUpdate | 更新状态/owner/字段/依赖/删除 | False | False | `Updated task #3 (status → in_progress)`;错误 is_error |

- **权限裁决**:四工具 `needs_permissions() → False` + `SYSTEM_TOOLS` 白名单(`modes.py:26` 追加 4 名)。理由:任务是 harness 内部状态(与 TodoWrite 同先例),模型直接调用,权限链对其审计无价值;`needs_permissions` 自声明是契约位,权限判断仍在引擎,不在工具内(主规格 #5)。
- **错误语义**:一切失败返回 `ToolResult(is_error=True, 文案)`,不抛异常(主规格 #2 自愈通道);未完成任务不阻塞工具使用(环/缺失只在 mutation 时拒绝,TaskList 照常列出)。

### 6.2 状态机与 owner 规则

```
pending → in_progress → completed
    └──────┘  (放弃:in_progress → pending,允许)
completed = 终态:任何非 completed → completed 允许;completed → 其他 拒绝("Task #N is completed")
deleted = 动作非状态:TaskUpdate status="deleted" → 真删文件(§3.4)
```

- **owner 规则裁决(阶段 11 简化)**:`owner: str | None` 可选字段;`status=in_progress` 时若显式传 owner 则写入,不传则保持现有值或 None;**不做自动分配**;owner 置空/释放不在 11 范围(认领后的交接语义属 13)。理由:自动分配(`agent swarms` 模式,TaskUpdateTool.ts:412-422)与 claimTaskWithBusyCheck 同为多代理语义,13 交付;11 单代理无「遗忘认领」问题。**多 in_progress 允许**(不设 TodoWrite 的单 in_progress 不变量):任务系统是面向多代理的存储,设单飞不变量会在 13 回退;单代理模型自然收敛。
- `completed` 终态是安全网:防模型误把已完成任务改回进行中,破坏「已完成 blocker 过滤」(§6.4)的显示语义。

### 6.3 TaskUpdate 字段面(镜像 Kode TaskUpdateTool)

```python
input_schema = {
    "type": "object",
    "properties": {
        "taskId": {"type": "string"},
        "subject": {"type": "string"},
        "description": {"type": "string"},
        "activeForm": {"type": "string"},
        "status": {"enum": ["pending", "in_progress", "completed", "deleted"]},
        "addBlocks": {"type": "array", "items": {"type": "string"}},      # 本任务阻塞的
        "addBlockedBy": {"type": "array", "items": {"type": "string"}},   # 阻塞本任务的
        "owner": {"type": "string"},
        "metadata": {"type": "object"},   # 键值合并;值传 null 删除键(镜像 Kode)
    },
    "required": ["taskId"],
}
```

- **依赖变更的语义裁决**:`addBlocks`/`addBlockedBy` 是**增量添加**(不提供 remove —— 需要移除依赖时用 `status=deleted` 删除,或接受依赖图的宽松性)。理由:Kode 面同样只有 add;remove 语义(显式列表替换)会让双向维护失去幂等性,且模型极少需要移除依赖。`validate_input` 拒绝:空串 id、`addBlocks` 与 `addBlockedBy` 同时含任务自身。
- 只读校验:未知 taskId → `Task not found`(TaskGet 亦如此,裁决:不存在即错误返回,不给空对象 —— 明确失败让模型不再猜测)。

### 6.4 TaskList 过滤

镜像 Kode `listTaskSummaries`(storage.ts:370-385):`blockedBy` 摘要**过滤已 completed 的 blocker 与不存在的任务**(仅保留「现存且未完成」的阻塞者)。理由:已完成的任务不再阻塞任何人,保留它会误导模型以为任务仍被阻塞。`_internal` 不引入(§1.2)。

```python
def summaries(self, task_list_id: str) -> list[TaskSummary]:
    tasks = self.list(task_list_id)
    done = {t.id for t in tasks if t.status == TaskStatus.COMPLETED}
    ids = {t.id for t in tasks}
    return [TaskSummary(id=t.id, subject=t.subject, status=t.status, owner=t.owner,
                        blockedBy=[b for b in t.blockedBy if b in ids and b not in done])
            for t in tasks]
```

### 6.5 工具文件样例(`tools/builtin/interaction/task_create.py`)

```python
"""TaskCreate: create a persistent task in the active task list."""

from __future__ import annotations

from ...base import Tool, ToolResult, ToolUseContext
from ....core.tasks import TaskStoreError, get_task_store

class TaskCreateTool(Tool):
    name = "TaskCreate"
    description = ("Create a task for multi-step work. Use an imperative subject "
                   "(\"Fix auth bug\" not \"Fixing auth bug\"); description must be "
                   "detailed enough for another agent to pick it up.")
    input_schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Imperative title"},
            "description": {"type": "string", "description": "Detail for handoff"},
            "activeForm": {"type": "string", "description": "Present-continuous spinner form"},
            "metadata": {"type": "object"},
        },
        "required": ["subject", "description"],
    }
    is_concurrency_safe = False  # mutates the store
    user_facing_name = "TaskCreate"

    def needs_permissions(self, input: dict) -> bool:
        return False  # harness-internal state; whitelisted in SYSTEM_TOOLS

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        try:
            task = await get_task_store().create(
                ctx.task_list_id,
                subject=str(input["subject"]).strip(),
                description=str(input["description"]).strip(),
                activeForm=input.get("activeForm"),
                metadata=input.get("metadata"),
            )
        except TaskStoreError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(f"Created task #{task.id}: {task.subject}")
```

`validate_input` 在 `create` 的 subject/description 校验之前先抛(空 subject → ToolError,走引擎校验路径);存储层仍自守(防御外部调用)。

## 7. TodoWrite 衔接与命名避让

### 7.1 裁决:并存,TodoWrite 保持内存原样

**裁决理由**:todo.md 11 的「todo」验收 = 与既有 todo 能力的**衔接不破坏**,而非改造。(a) 03 契约被测试锁定(单列表幂等替换、单 in_progress 不变量、`needs_permissions=False`、`reset_todos` 测试钩子)——任何持久化改造都要重写 `_STORE` 语义与全部 9 个测试,破坏 V1 已交付契约;(b) 两者场景不同:TodoWrite 是轻量会话内清单(模型每轮整体重写,适合简单多步),Task 系统是持久化任务图(多代理协调、依赖追踪)——**Kode 自身两者并存**(`interaction/TodoWriteTool` 与 Task 系列同目录),非互斥;(c) 用户的 TodoWrite 依赖(如果接入系统提示词)不因 11 而失效。合并收益(少一个工具)远小于成本。

**未来出口**:若 Task 系统成熟后 TodoWrite 冗余,在 19 插件化收尾阶段评估移除(写进 §12)。

### 7.2 命名避让裁决

- **工具名**:新四工具 `TaskCreate`/`TaskGet`/`TaskList`/`TaskUpdate` —— 与既有 `TaskOutput`/`TaskStop`(`tools/builtin/system/task.py`,后台 Bash)在工具名空间上**无冲突**(注册表按名 key,`registry.py:21`)。
- **模块名**:新工具放 `tools/builtin/interaction/task_create.py` 等 4 文件(类别子包 interaction/,与 todo.py 同层平铺);`core/tasks/` 包放存储与图逻辑。**不用** `tasks.py`/`TasksTool` 单文件名(与 `system/task.py` 平级重名风险、且违反每工具一文件规范)。
- **测试文件名**:`tests/tools/test_task_create.py` 等 —— 与既有 `tests/tools/test_task.py`(TaskOutput/TaskStop)不冲突。

### 7.3 与 TodoWrite 的共享与隔离

共享:无(存储独立)。隔离:`TodoWrite._STORE` 内存 dict 不动;`SYSTEM_TOOLS` 追加 4 名不影响 TodoWrite 白名单条目;系统提示词若已引导 TodoWrite,11 不改 base_prompt(13 接任务引导时统一定稿,§1.2 裁剪项)。

## 8. 装配接线

### 8.1 taskListId 解析(5 层裁剪为 3 层 + 扩展点)

镜像 Kode `getTaskListId`(storage.ts:149-159)五层:env > 进程内队友 team name > 进程式队友 env > leader team name > session id 兜底。**11 实现前三层的可扩展底座**:

```python
# core/tasks/storage.py
def resolve_task_list_id(explicit: str | None = None) -> str:
    """explicit 参数 > CODESAGE_TASK_LIST_ID env > "default" 兜底。
    阶段 13 在此插入 team name 层(teammate 共享同一列表)。"""
    if explicit:
        return explicit
    return os.getenv("CODESAGE_TASK_LIST_ID", "").strip() or "default"
```

**成文契约**:`TaskStore.create` 等入口在锁外先调 `resolve_task_list_id(ctx.task_list_id)`(explicit 即工具传入的 ctx 值,env 层因此对工具路径生效),锁内再用解析结果定位目录 —— env 覆盖链对全部工具路径有效,而非仅测试。

真正的隔离注入走 **`ToolUseContext.task_list_id`**:

```python
# tools/base.py:33 —— ToolUseContext 新增字段(带默认值,零破坏)
task_list_id: str = "default"
```

引擎在唯一构造点注入 session id(`engine/loop.py:744`,全包唯一 ToolUseContext 构造点):

```python
self._tool_ctx = ToolUseContext(cwd=self.cwd, abort_event=self.abort,
                                task_list_id=self.session.session_id if self.session else "")
```

**成文注记(S4 实施同步,2026-08-08)**:实现用 `self.session.session_id if self.session else ""` 而非字面 `self.config.session.session_id` —— (a) AgentLoopConfig.session 是 Optional,无 session 的既有测试路径须保留(None 守卫与文件内 `if self.session else ""` 惯用式一致);(b) 兜底传空串(falsy)而非 `"default"`:`resolve_task_list_id` 的 `explicit 为空 → env > default` 分支因此在引擎路径仍可达,`"default"` 字面会死代码掉 env 覆盖层(§8.1「env 覆盖链对全部工具路径有效」成文契约)。两分支均符合解析链 explicit > env > default;R8 兜底(无 session 无 env → "default")不变。

`session_id` 已在 `AgentLoopConfig.session`(assemble.py:76 装配,`_new_session_id()` 生成)。效果:独立会话各自隔离(任务列表按会话 id 分层,对齐 Kode「会话靠 session ID」);`CODESAGE_TASK_LIST_ID` env 显式覆盖(测试与外部工具);13 在此链条加 team name。

**兜底裁决**:解析链最终兜底 `"default"` 而非 session id —— 因为引擎构造点是唯一真实路径,session id 恒可注入,兜底只服务于「脱离引擎直接调用工具」的测试场景;若未来出现无 session 的调用方,`"default"` 与其共享列表,风险表 R8 记录。

### 8.2 注册与白名单

- `tools/builtin/__init__.py:16` BUILTIN_TOOLS 追加 4 个实例。
- `permissions/modes.py:26`:`SYSTEM_TOOLS = frozenset({..., "TaskCreate", "TaskGet", "TaskList", "TaskUpdate"})`(只增不删)。
- 写保护:默认 `config_dir = ~/.codesage` 下任务目录含 `.codesage` 组件,已被 `WRITE_PROTECTED_COMPONENTS`(`permissions/paths.py:17`,大小写不敏感)覆盖 —— 模型无法用 Write/Edit 直写任务文件,零改动;**注意**:若用户以 `CODESAGE_CONFIG_DIR` 重定向到不含 `.codesage` 的目录,写保护不自动跟随,靠 §5.3 读取时诊断兜底(非安全洞);如需要,05 阶段把 `"tasks"` 加进 `WRITE_PROTECTED_DIRS`(paths.py:29)一行解决(属 05 模块改动,需回归)。

## 9. 测试计划

### 9.1 镜像清单(`tests/…`,镜像实现文件)

| 测试文件 | 镜像 | 用例要点 |
|---|---|---|
| `tests/core/tasks/test_graph.py` **新增** | `core/tasks/graph.py` | 四类验证矩阵:正常图 valid=True;环(三角环、自环经文件编辑、环规范化首成员重复);missing(blocks 引用不存在 / blockedBy 引用不存在,declaration 标注);duplicate(同 id 双文件);asymmetric(单端声明,valid 仍 True);纯函数无 IO(直接喂 Task 列表) |
| `tests/core/tasks/test_storage.py` **新增** | `core/tasks/storage.py` | CRUD 全链路(tmp_path 数据根);一任务一文件落盘形状;ID 自增 + 高水位(删除最大 ID 后新任务不重用);损坏任务文件跳过不致命;删除清引用(双向);completed 终态拒绝回退;TaskList 过滤 completed blocker;addBlocks/addBlockedBy 双向 + 幂等;自环拒绝;环拒绝(mutation);missing 依赖拒绝;sanitize(非法 task_list_id 字符) |
| `tests/core/tasks/test_lock.py` **新增** | `storage.py::_dir_lock` | O_EXCL 持锁互斥(同进程二次获取 EEXIST);stale 回收(mtime 伪造超时);重试超限抛 TaskStoreError;锁文件 finally 清理;asyncio 交错下 mutation 原子性(gather 并发 create N 个,ID 无重复无丢失) |
| `tests/tools/test_task_create.py` **新增** | `interaction/task_create.py` | 创建成功输出;空 subject 校验;TaskStoreError → is_error |
| `tests/tools/test_task_get.py` **新增** | `interaction/task_get.py` | 存在返回 JSON;不存在 is_error "Task not found" |
| `tests/tools/test_task_list.py` **新增** | `interaction/task_list.py` | 摘要行格式(owner/blocked 标注);空列表 "No tasks found";completed blocker 不出现在 blockedBy 标注 |
| `tests/tools/test_task_update.py` **新增** | `interaction/task_update.py` | 各字段更新;status="deleted" 真删;环/自环/missing 错误文案;needs_permissions() is False |
| `tests/engine/test_loop.py` **追加** | 装配 + 四工具 | E2E:mock LLM 脚本 TaskCreate×3 → TaskList(摘要)→ TaskUpdate(完成);断言任务文件落盘、状态流转、task_list_id 按会话隔离 |
| `tests/core/test_session.py` / `tests/tools/test_task.py` | 04 / 03 既有 | 回归(零改动) |

### 9.2 不能破坏的既有契约(11 改动红线)

| 红线 | 锚点 | 说明 |
|---|---|---|
| TodoWrite 03 契约(内存幂等替换、单 in_progress、白名单) | `interaction/todo.py`、`tests/tools/test_todo.py` | **零改动**,并存裁决 §7;回归全绿 |
| TaskOutput/TaskStop 后台任务契约 | `system/task.py`、`tests/tools/test_task.py` | 工具名/模块名避让,零改动 |
| ToolUseContext 既有构造点 | `engine/loop.py:744` | 新字段带默认值,既有构造零破坏;装配注入 session id 后行为变化仅限任务工具 |
| 权限链决策顺序与审计 | `permissions/engine.py` | 四工具走 SYSTEM_TOOLS 白名单短路(与 TodoWrite 同路径),每决策恰一条审计事件语义不变 |
| concurrency 屏障 | `engine/tool_queue.py` | 四工具 is_concurrency_safe 声明正确(读 True/写 False),屏障语义不变 |
| 会话 append-only | `core/session.py` | 任务不落会话文件,04 契约零改动(§2 裁决的直接结果) |
| 写保护路径 | `permissions/paths.py` | 任务目录依赖既有 `.codesage` 组件保护,不新增清单条目 |
| audit/hooks JSONL 流 | `permissions/audit.py`、`hooks/` | 零改动 |

### 9.3 回归

`python -m pytest tests/ -q` 全绿(基线 838,新增后增长);含 03/04/05/06/09/10 全部既有用例。

## 10. 实施步骤

| 步 | 内容 | 文件:锚点 | 闸门 |
|---|---|---|---|
| S1 | 契约层 + 只读图验证(纯函数) | `core/tasks/__init__.py`、`core/tasks/types.py`(Task/TaskSummary/TaskStatus/TaskUpdate)、`core/tasks/graph.py`(validate_task_graph) **全新增** | `test_graph.py` 绿 + types 断言(落 test_graph.py/test_storage.py 内,无 IO,先行) |
| S2 | 存储层:CRUD + 高水位 + 双向依赖 + mutation 环预防 + `_dir_lock`(含 gather 并发用例) | `core/tasks/storage.py`(TaskStore;`_dir_lock` 私有;`resolve_task_list_id`);`config/atomic.py` 复用 | `test_storage.py` + `test_lock.py` 绿(纯存储,无工具;并发原子性在此测,因锁定义处) |
| S3 | 四工具 + 注册 + 白名单 | `tools/builtin/interaction/task_{create,get,list,update}.py` **新增**;`tools/builtin/__init__.py:16`;`permissions/modes.py:26` | 工具 4 测试绿;`test_registry.py` 回归 |
| S4 | 装配:ToolUseContext.task_list_id + 引擎注入 + E2E | `tools/base.py:33`(加字段);`engine/loop.py:744`(注入 session_id);`cli/assemble.py`(无改动,验证);`test_loop.py` 追加 E2E(四工具全链 + 会话隔离) | 全量回归绿;E2E 绿(任务落盘/状态流转/隔离断言) |
| S5 | 红线固化 + 理解文档 | `docs/modules/11-tasks.md` | 全量回归绿(838 + 新增);TodoWrite 全测试原样通过 |

依赖:S1 → S2 → S3 → S4;S5 收尾。步骤间每步独立提交。依赖外部:03 工具契约、04 会话(session_id)、05 权限白名单、09 无交互 —— 全部已就绪,零新依赖(Ask first 不触发)。

## 11. 风险与边界

| # | 风险 | 缓解 |
|---|---|---|
| R1 | Windows 文件锁语义(`open("x")` 的 EEXIST 与 POSIX 差异、AV 扫描瞬时占用) | `_dir_lock` 单测在 CI(Windows)固化 O_EXCL/EEXIST 行为;atomic_write 的 Windows EPERM 重试先例(config/atomic.py:41-46)证明此类问题已有处理路径 |
| R2 | 跨进程锁冲突时 `time.sleep` 阻塞事件循环 | 阶段 11 单进程实际不可达(进程内 asyncio.Lock 先单飞);总等待上限 1.5s;13 引入真实跨进程前把锁获取移入 `asyncio.to_thread`,API 不变 |
| R3 | 目录级锁死(stale 误判:活锁持锁超 10s 被回收,双写者) | 任务写操作 µs-ms 级,10s 远超;锁文件含 pid+时间戳,13 可升级为 pid 活性检查(Kode 同款演进) |
| R4 | 环检测性能(mutation 时 DFS) | O(V+E),数百任务 µs 级;mutation 只跑单路径 DFS,不做全量验证;全量验证仅测试/诊断调用 |
| R5 | TodoWrite 兼容破坏(03 契约) | §7 并存裁决成文;红线表 9.2 第一行;`test_todo.py` 零改动回归 |
| R6 | completed 终态与 13 的 `unassignTeammateTasks`(in_progress 回 pending) | 13 只回退非 completed 任务(镜像 Kode storage 语义),与终态不冲突;§6.2 已按此设计 |
| R7 | 进程崩溃残留锁文件/半写状态 | 锁文件 stale 回收(§4.2);任务文件原子写(读端永不见半写);高水位写失败降级 best-effort + 文件扫描兜底 |
| R8 | taskListId 兜底 `"default"` 的会话串扰 | 引擎装配注入 session id 后真实路径不经过兜底;env 显式覆盖优先;测试直接调工具时共用 "default" 仅为测试便利,写进理解文档 |
| R9 | taskListId sanitize 碰撞("a/b" 与 "a-b" 静默归并同一目录) | 与 Kode sanitizeTaskListId 同款行为,接受;列表隔离靠调用方不传冲突 id(session id/team name 均只含安全字符),写进理解文档 |

## 12. 与路线图的关系

- **主规格 Open Question(198 行)落定**:「任务持久化层级」= 独立项目级存储 `{config_dir}/tasks/`(§2),04 会话零改动;12 会话生命周期不受影响(任务跨会话可见)。
- **11 → 12**:任务文件生命周期长于会话;12 的 fork/resume 天然共享 taskListId(session id 派生),无需任务迁移。
- **11 → 13(子代理)**:本阶段交付存储 + 四工具 + 锁基座;13 在其上添加:taskListId 解析的 team name 层、自动 owner 分配、claimTaskWithBusyCheck(目录锁内 busy 检查 + 原子认领 —— 锁基座已备)、unassignTeammateTasks 退出清理、Mailbox 通知、`getReadyTasks`/`getCriticalTaskBlockers` 调度视图(graph.py 纯函数扩展)。13 系统提示词一并接任务引导(§1.2 裁剪项)。
- **与 09 hooks 的 Task* 事件预留衔接(§1.2 裁剪项成文)**:09-hooks.md:34 将 `Subagent*/Task*/TeammateIdle` 列为八事件之外「评估后的扩展来源」,注明「依赖阶段 13/11(未建)」—— 裁决:**Task* hook 事件由 13 触发与消费**(多代理跨进程同步语义,Mailbox/团队通知才有消费者),11 不新增事件。11 承担的准备:storage 层** mutation 单点**(每次写操作恰好一处返回点),13 加事件时 emit 点已定、零重构;09-hooks.md:106 内容规则(无内容字段工具 → 工具级匹配)对四新工具自动生效,零改动。
- **主规格留痕(修订)**:codesage.md:87「agents/ # 13 子代理(定义解析 + Task 工具)」与路线图 152 行(11 = 任务系统)存在归属矛盾 —— 已裁决 Task 工具本体与存储落 **11**(§1.2),13 只做多代理扩展;已同步修订 codesage.md:87 注记。06-engine.md:29「不做:…子代理 Task 工具(13)」是 06 的范围边界声明(06 不做),不构成排期约束,与本裁决不冲突。
- **10-compact §1.2 复核**:「Task 快照注入摘要 ❌(CodeSage 无官方式 task list)」—— 本阶段引入持久化 task list 后复核:**保持原裁决**。PreCompact instructions 通道已覆盖摘要诉求,注入任务快照会破坏「摘要消息为唯一边界载体」(10 §8.1);但 TodoV2 的**周期性提醒注入**(10 轮,isMeta system-reminder)是新发现诉求,不属快照注入 —— 裁决:不纳入 11,13 子代理场景再评(§1.2 已列)。
- **11 → 19(插件化收尾)**:TodoWrite 与 Task 系统并存状态的存废评估,19 时定(§7.1)。
- **阶段 11 自身后向依赖**:03(工具契约/注册)、04(session_id 装配)、05(SYSTEM_TOOLS)、08(is_reminder 通道,仅未来提醒注入用)—— 全部已交付。

---

*附:探索依据(2026-08-08)w1-ref-doc 通读 `docs/reference/task-system.md`(594 行,Claude TodoV2 全量设计)+ w2-codesage 扫描(codesage 现网代码:TodoWrite 种子带持久化 ponytail 注释、TaskOutput/TaskStop 命名占用、atomic.py/read_json_lossy/写保护组件全现成)+ w3-spec-format(主规格 81/198 行、10-compact §1.2 裁决、03 交付清单)。对照 Kode-CLI `packages/core/src/tasks/storage.ts`(一任务一文件 + O_EXCL 锁 + stale 10s + 高水位 + tombstones + mutation 环预防)、`automation/taskGraph.ts`(只读四类验证)、`TaskUpdateTool.tsx`(deleted 动作、metadata null 删除、owner 自动分配在 swarms 开关下)。对照结论:CodeSage 单进程 + 单 store root 使 tombstones 与任务级细锁可裁剪;环预防(DFS)与四类验证(读取时)双轨是 Kode 已证方案,照抄而非重设计;TodoWrite 并存有 Kode 自身先例。*
