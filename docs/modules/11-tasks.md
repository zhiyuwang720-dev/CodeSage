# 阶段 11:任务系统(理解文档)

> 权威设计:`docs/specs/11-tasks.md`(实现时逐字执行)。本文是设计摘要 + 决策记录 + 实现期关键裁决(S1-S5 全部交付,918 测试全绿,2026-08-08)。

## 设计摘要

持久化任务系统:Task CRUD + blocks/blockedBy 依赖 + 环检测,独立于 TodoWrite(内存原样并存,§7)。任务 = harness 内部状态,四工具 `needs_permissions() → False` + SYSTEM_TOOLS 白名单(与 TodoWrite 同先例),引擎直调。

- **持久化层级裁决**(主规格 Open Question 落定):独立项目级存储 `{config_dir}/tasks/{task_list_id}/`,一任务一 JSON;04 会话零改动、12 不受影响。
- **结构**(镜像目录规范):`core/tasks/` = 契约层(types.py:Task/TaskSummary/TaskStatus/TaskUpdate)+ 实现层(graph.py 纯函数验证、storage.py TaskStore)+ 入口层(`__init__.py` 导出);`tools/builtin/interaction/task_{create,get,list,update}.py` 每工具一文件;测试镜像 `tests/core/tasks/test_{graph,storage,lock}.py` + `tests/tools/test_task_{create,get,list,update}.py` + `test_loop.py` E2E。
- **存储**(§3):一任务一 JSON(snake_case,`model_dump_json(indent=2)` 落盘);ID 自增 + `.highwatermark`(删最大 ID 后不重用);删除 = 真删 + 双向引用清理(否决墓碑);损坏任务文件跳过不致命(`read_json_lossy`,文件名即 id 权威);写入原子(tmp+rename+fsync,复用 config/atomic.py)。
- **锁**(§4):双层 —— 进程内 `asyncio.Lock`(mutation 持,读不持)+ 跨进程 O_EXCL 目录锁(`_dir_lock`:30 次 × 50ms,stale 10s 回收,poison 锁写失败清理,finally pid 校验后删锁);**单粒度目录级**(任务级细锁 13 吞吐需要时再加,API 已预留);update 锁内重读(读-改-写契约,防丢失更新)。
- **依赖与环**(§5):双向维护幂等 `_add_edge`(blocks 与 blocked_by 同批「逐边先查后加」,防同批 2-环);mutation 三查:自环 "Task #N cannot depend on itself" → 存在性 "Task not found: N" → 成环 "Adding dependency S -> T would create a cycle";completed 终态拒绝回退;读取时全量验证纯函数 `validate_task_graph`(duplicate/missing/cycles/asymmetric,DFS 三色)。
- **四工具**(§6):`needs_permissions() → False`(harness 内部状态,与 TodoWrite 同先例);错误全 `ToolResult(is_error=True)` 不抛(主规格 #2 自愈);input_schema camelCase(taskId/addBlocks/…)↔ TaskUpdate snake_case 映射,metadata 值传 null 删键。契约表:

| 工具 | 职责 | concurrency | 输出 |
|---|---|---|---|
| TaskCreate | 创建任务(subject/description 必填) | False(写) | `Created task #N: <subject>` |
| TaskGet | 单任务详情(taskId 必填) | True(读) | 单行 JSON 全字段 |
| TaskList | 任务摘要列表(无参数) | True(读) | 行 `#3 [pending] Fix auth (alice) [blocked by #1, #2]`,ID 升序;空 → `No tasks found` |
| TaskUpdate | 更新字段/状态/依赖/删除(taskId 必填) | False(写) | 按实际变化标注 `(status → in_progress)`、`(owner → alice)`、多字段逗号连接、无变化 `(ok)`;`status="deleted"` → `Deleted task #N` |

错误文案(§5.2):自环 `Task #N cannot depend on itself` / missing `Task not found: N` / 成环 `Adding dependency S -> T would create a cycle` / completed 终态 `Task #N is completed`。
- **装配**(§8):`ToolUseContext.task_list_id: str = "default"`(带默认值零破坏);引擎唯一构造点注入 `self.session.session_id if self.session else ""`;解析链 `resolve_task_list_id(explicit 参数 > CODESAGE_TASK_LIST_ID env > "default")` 在 TaskStore 入口锁外调用;cli/assemble.py 零改动。

## 设计决策记录

1. **持久化层级 = 独立项目级存储** — 否决挂会话存储(fork 断链/无锁 update)、cwd 项目级(跨 cwd 失效)、sqlite(主规格 #14 JSON+文件锁);04 会话零改动。
2. **一任务一 JSON + 高水位 ID** — 对齐 Kode TodoV2;真删 + 引用清理否决墓碑(单 store root 无复活路径);高水位防 ID 重用(文件扫描 + 高水位取 max),写失败 best-effort 降级,唯 delete 中止(高水位必须随删更新)。
3. **双层锁 + 单粒度目录级** — 进程内 asyncio.Lock 挡同进程并发,跨进程 O_EXCL 挡多进程;任务级细锁 13 吞吐需要时再加(API 已预留)。
4. **环检测双轨** — mutation 预防(`_has_path` 单路径 DFS)+ 读取时全量验证(纯函数,测试/诊断用);mutation 只跑单路径不做全量。
5. **Task 工具属 11,13 只加多代理层** — 13 交付:team name 解析层/自动 owner/claimTaskWithBusyCheck/unassign/Mailbox/调度视图;多 in_progress 允许(不设 TodoWrite 单飞不变量,13 多代理语义)。
6. **TodoWrite 并存** — 内存原样零改动(§7 裁决,Kode 自身同例);命名避让 `task_*.py`/`core/tasks/`;红线表固化。
7. **owner 规则 11 简化** — 可选字段,显式传则写、不传保持,无自动分配/无释放;认领交接语义属 13。
8. **TaskList 过滤 completed blocker** — 仅保留「现存且未完成」阻塞者(已完成不再阻塞任何人,§6.4 镜像 Kode listTaskSummaries)。
9. **completed 终态安全网** — 防模型误把已完成任务改回,破坏 blocker 过滤显示语义;13 的 unassign 只回退非 completed(§6.2 已按此设计,R6)。
10. **依赖变更增量添加** — addBlocks/addBlockedBy 无 remove(remove 会破坏双向维护幂等性;移除依赖用 status=deleted 或接受宽松图)。
11. **不存在即错误** — TaskGet/未知 taskId → `Task not found: N` is_error,不给空对象;明确失败让模型不再猜测。
12. **R8 兜底 "default" 的定位** — 兜底只服务于「脱离引擎直接调用工具」的测试场景;引擎真实路径恒注入 session id;env 显式覆盖优先。测试直调工具时共用 "default" 列表仅为测试便利,非并发使用。
13. **R9 sanitize 碰撞接受** — `[^A-Za-z0-9_-]+ → "-"`("a/b" 与 "a-b" 静默归并,与 Kode sanitizeTaskListId 同款);列表隔离靠调用方不传冲突 id(session id/team name 均只含安全字符);空结果兜底 "default"(防根目录污染)。

## 实现期关键裁决(S1-S5,review 驱动落地)

14. **S1:id 规范化** — 镜像 Kode cloneTask:trim、空串跳过、空 id 计重复、list 去重;环规范化取字典序最小旋转且首成员重复。
15. **S2:API 三对齐** — update 收 `TaskUpdate` 模型(非 kwargs)、`get → Task | None`(工具层转 is_error)、`resolve_task_list_id` 在 6 个公共入口锁外调用(env 覆盖链对工具路径有效)。
16. **S2:高水位写失败 → delete 中止** — `_write_highwatermark` 返回 bool,写失败在 unlink 前 raise(防删除后 ID 重用);其他写路径 best-effort。
17. **S2:锁健壮性** — `_dir_lock` 首行 `dir.mkdir(parents=True, exist_ok=True)`(update/delete 不建目录的路径共用一守卫);poison 锁文件写失败即清理再 raise;finally 删锁前 pid 校验(防误删后继者锁,TOCTOU 残余窗口注释在案);sanitize 空结果 → "default"。
18. **S3:validate_input 不在 Tool.call() 基类** — 引擎在校验路径调用;工具测试直调 `validate_input()` 断言先抛(test_todo.py 同模式)。
19. **S3 P3-2:显式 None 拒绝** — `str(None)` 会以 "None" 字符串入库;create/update 的 subject/description 显式出现必须非空 str,未传(update 可选字段)不拦。
20. **S3:工具读同步写异步** — storage get/list/summaries 同步(读不持锁),create/update/delete 异步;工具按各自 await。
21. **S3:TaskUpdate 动态字段标注** — 输出按实际变化字段(镜像 Kode updatedFields 顺序),status 带箭头值,块/元数据只标名,无变化 `(ok)`;spec §6.1 只钉死 status 形态,review 要求字段名按实际变化。
22. **S4:注入空串而非 "default"** — `task_list_id=self.session.session_id if self.session else ""`:无 session 时空串(falsy)让 resolve 的 env > default 分支在引擎路径仍可达,"default" 字面会死代码掉 env 覆盖层;spec §8.1 已回写成文注记。
23. **S4 E2E:ID 按列表目录独立分配** — 高水位 per-dir,新会话目录从 1 重新开始;E2E 断言按目录而非全局 ID。
24. **S4 E2E 隔离** — `monkeypatch storage._store = TaskStore(tmp_path)` 单点替换模块级单例(工具经引擎完整路径执行:注册表 → 权限 → tool_queue → _run),不切 config_dir;TaskList 只读不创建任务目录。

## 风险与边界(R1-R9,spec §11)

| # | 风险 | 缓解(实现期状态) |
|---|---|---|
| R1 | Windows 文件锁语义(EEXIST/POSIX 差异、AV 瞬时占用) | `_dir_lock` 单测在 Windows CI 固化 O_EXCL/EEXIST;atomic_write EPERM 重试先例(config/atomic.py) |
| R2 | 跨进程锁冲突 `time.sleep` 阻塞事件循环 | 11 单进程实际不可达(进程内锁先单飞);总等待上限 1.5s;13 引入真实跨进程前移入 `asyncio.to_thread`,API 不变 |
| R3 | 目录级锁死(stale 误判活锁) | 写操作 µs-ms 级,10s 远超;锁文件含 pid+时间戳,13 可升级 pid 活性检查 |
| R4 | 环检测性能 | O(V+E),mutation 只跑单路径 DFS;全量验证仅测试/诊断 |
| R5 | TodoWrite 兼容破坏 | §7 并存裁决;红线表 9.2;`test_todo.py` 零改动回归 ✓ |
| R6 | completed 终态 vs 13 unassign | 13 只回退非 completed(镜像 Kode),不冲突 |
| R7 | 崩溃残留锁/半写 | stale 回收 + 原子写 + 高水位 best-effort + 文件扫描兜底 |
| R8 | 兜底 "default" 会话串扰 | 引擎真实路径不经过兜底(session id 恒注入);env 显式覆盖优先;测试共用 "default" 仅为便利(决策 12) |
| R9 | sanitize 碰撞("a/b"→"a-b" 归并) | 与 Kode 同款,接受;调用方不传冲突 id(session id/team name 只含安全字符)(决策 13) |

## 与后续阶段衔接

- **12 会话生命周期**:零改动 —— 任务不落会话文件(持久化层级裁决的直接结果);fork/resume 不复制任务(任务属列表非会话)。
- **13 子代理**:Task 工具本体已落 11;13 只加 team name 解析层(在 `resolve_task_list_id` 的 explicit/env 之间插入,teammate 共享列表)、自动 owner、claimTaskWithBusyCheck/unassign/Mailbox/调度视图;Task* hooks(事件级集成)11 不做(09 八事件固定),13 再评;锁升级路径 R2(to_thread)/R3(pid 活性检查)在 13 引入真实跨进程时落地。
- **10 压缩**:Task 快照注入摘要复核保持 ❌(阶段 11 不做);周期性提醒注入(10 轮)13 再评。
- **TodoWrite**:并存至任务系统替换其能力为止(TodoWrite 保持内存原样,红线表固化)。

## 测试概览与红线验证(2026-08-08)

- **测试数字**(收集口径):基线 838 → S1 860(+22)→ S2 893(+33)→ S3 924(+31:create 7 + get 5 + list 6 + update 13)→ S4 927(+3 E2E)→ S5 927(文档步无新测试);全量 `python -m pytest codesage/tests/ -q` = **918 passed, 9 skipped, 0 fail**;红线套件 `test_todo.py + test_task.py + test_session.py` 原样通过(27 passed,零改动)。
- **改动文件清单**(`git diff master..HEAD --stat` 实测):23 文件 **+1914/-7**,全部在范围内 —— `core/tasks/*`(新增)、`tools/base.py`(+1 字段)、`tools/builtin/interaction/task_*.py`(新增)、`tools/builtin/{__init__,interaction/__init__}.py`、`permissions/modes.py`(白名单只增 4 名)、`engine/loop.py`(构造点 +1 参数)、`tests/*`(新增/期望更新)、`docs/modules/11-tasks.md`。
- **零触碰**:Kode-CLI/backend(0 文件)、TodoWrite(todo.py/test_todo.py)、TaskOutput/TaskStop(system/task.py/test_task.py)、04 会话(session.py/test_session.py)、权限引擎/审计(engine.py/audit.py)。
