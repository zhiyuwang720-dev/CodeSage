# CodeSage 任务清单

> 对应 `docs/specs/codesage.md` 阶段路线图。每个任务 = 一个分支 + 阶段规格 + 实现 + 文档 + 合并。

- [x] **01 config 配置系统** (`feat/01-config`)
  - 验收:settings 三层(user/project/local)加载与覆盖正确;全局配置读写;AGENTS.md 路径发现
  - 验证:`pytest tests/ -q`;针对覆盖优先级的单测
- [x] **02 ai LLM 客户端** (`feat/02-ai`)
  - 验收:OpenAI 兼容 + Anthropic 原生双 adapter;流式;重试(retry-after);模型指针 main/task/compact/quick;VCR 录制回放;成本核算
  - 验证:单测(离线 mock)+ VCR 回放测试
- [x] **03 tools 工具契约与内置工具** (`feat/03-tools`)
  - 验收:Tool 三合一对象;注册表;Read/Write/Edit/Glob/Grep/LS/Bash(真超时/kill);超大结果落盘
  - 验证:每工具单测;Bash 超时/kill 实测
- [x] **04 core 消息与会话** (`feat/04-core`)
  - 验收:Message 类型;normalizeMessagesForAPI(合并/剔除);会话 JSONL append-only + 原子写
  - 验证:归一化规则单测(含边界:相邻 user 合并、tool_result 前置)
- [x] **05 permissions 权限引擎** (`feat/05-permissions`)
  - 验收:决策链完整顺序;deny>ask>allow;路径规则(gitignore 语义 + symlink 展开);写保护路径;plan/default/yolo 三模式;**审计钩子**
  - 验证:决策链矩阵单测;审计事件断言
- [x] **06 engine 引擎主循环** (`feat/06-engine`)
  - 验收:主循环(递归或显式迭代,见 R1);ToolUseQueue 并发屏障;错误转 tool_result;AbortSignal 三检查点;hooks 挂接点
  - 验证:循环终止单测;**>2000 轮压力测试**
- [x] **07 cli CLI REPL** (`feat/07-cli`)
  - 验收:交互循环;权限询问(文本);信号处理;流式输出 → **V1 闭环验收**
  - 验证:端到端小任务;V1 验收清单(见 plan.md)
- [x] **08 context 上下文工程** (`feat/08-context`)(规格:`docs/specs/08-context.md`)✅ 2026-08-06
  - 验收:AGENTS.md 逐层收集 + 32KB 截断 + override;system prompt 分层组装(静态 base + reminder 注入);system-reminder(上限 10);git 快照(CC-14);上下文 memoize(CC-13);token 预算(usage 锚点);结构化 auto-compact(PI-05:turn 边界 + split-turn 前缀摘要 + fileOps);旧工具结果清理;压缩后最近文件恢复
  - 验证:上下文组装单测 + 压缩边界单测 + VCR 集成;**471 passed, 9 skipped**
  - 步骤:S1 消息契约(is_reminder/is_compaction_summary + normalize)→ S2 tokens.py → S3 context.py → S4 注入接线 → S5 compaction 核心 → S6 loop 接线 → S7 恢复/清理 → S8 收尾
- [x] **09 hooks 钩子系统** (`feat/09-hooks`)(规格:`docs/specs/09-hooks.md`)✅ 2026-08-07
  - 验收:八事件(SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop/PreCompact/PostCompact/Notification);钩子先于权限引擎(deny 优先/allow 短路/updatedInput 透传 + 写保护地板);safetyCheck bypass-免疫位(仅 hook allow 可设);命令+提示+HTTP 三执行体;if 条件(hook 级,复用权限规则语法,仅 PreToolUse/PostToolUse 可求值,工具不存在/校验失败恒 false,matcher 先 if 后);compact 事件(封装进 `_compact`:auto 主路径 + PTL 路径一处覆盖;PreCompact exit 2 阻止压缩、stdout 多钩子 join 注入摘要 prompt、fail-open;PostCompact 纯观察型);Notification 四通知源(permission_request/permission_denied/tool_error/llm_error,全 fail-open + 默认 10s,statusbar 消费,不产生权限审计事件);JSON 结果解析;fail-closed(PreToolUse 超时/JSON 失败 → deny);双流审计(权限 audit.jsonl + hooks.jsonl);执行引擎管线(§4.10:无钩子零开销短路(事件→钩子数索引)/执行层去重(同批只执行/审计一次)/stdout 限额(256KB 截断 + UTF-8 errors=replace)/聚合输出传递链(逐事件消费总表))
  - 验证:决策合并矩阵单测 + 执行器单测(test_command/test_prompt/test_http)+ 事件接线单测(test_compact_events/test_notification)+ if 单测(test_if_rules);**515 全绿 + 09 新增**
  - 步骤:S1 契约层(types/base)→ S2 匹配与解析+if 过滤(_common)→ S3 命令执行体 → S4 HTTP 执行体(http.py)→ S5 HookManager(含 notify)→ S6 引擎接线(floor_check)→ S7 事件接线 → S8 compact 事件接线 → S9 通知 emit → S10 提示执行体+装配 → S11 收尾
  - 验证:压缩边界单测
- [x] **10 compact 上下文压缩增强** (`feat/10-compact`)(规格:`docs/specs/10-compact.md`)✅ 2026-08-08
  - 验收:错误分类/扣留层(413/PTL + max_output_tokens/length 统一归「可恢复」,其余走原路径);输出端恢复(stop_reason=="length" 残缺 tool_use 截断重发);恢复阶梯 + 防死循环闸(每错误类每 turn 至多一次);显式轮次状态 + transition reason(RunState.last_transition/recovery_attempts);manual `/compact` 命令(loop.compact_now() + commands.py 注册,trigger="manual",PreCompact 钩子按 trigger 匹配);熔断复位/闭包化(成功即复位,manual 恒可用,硬阻塞语义);boundary 消息模式成文(core/normalize.py 保位,测试固化)
  - 验证:分类器单测(test_errors)+ 恢复/闸门单测 + manual 命令测试 + 熔断闭包回归 + boundary 固化;**全量回归 829 全绿**
  - 步骤:S1 分类器 → S2 RunState 扩展 → S3 输出端恢复 → S4 恢复阶梯统一闸门 → S5 熔断闭包化 → S6 compact_now() → S7 /compact 注册 → S8 transition 日志 → S9 boundary 固化 + modules 文档
- [x] **11 tasks 任务系统** (`feat/11-tasks`)(规格:`docs/specs/11-tasks.md`)✅ 2026-08-08
  - 验收:Task CRUD 全链路(TaskCreate/Get/List/Update 四工具 + `{config_dir}/tasks/{task_list_id}/` 持久化);blocks/blockedBy 双向依赖 + 环检测(mutation 三查预防 + validate_task_graph 四类全量验证);TodoWrite 并存零破坏;命名避让(TaskOutput/TaskStop);E2E(mock LLM 单轮 run TaskCreate×3→TaskList→TaskUpdate,会话隔离);双层锁(进程内 asyncio.Lock + 跨进程 O_EXCL 目录锁)
  - 验证:依赖图单测(环/缺失/重复/非对称)+ 存储单测(CRUD/高水位/并发)+ 锁单测(真实竞态)+ 工具 4 文件 + E2E;**918 passed, 9 skipped**
- [x] **12 session 会话生命周期** ✅ 2026-08-14(`feat/12-session`)
  - 验收:fork/continue/resume;sidechain 日志;归档
  - 验证:会话恢复单测(摘要前 2 条 user 消息);**1008 passed, 9 skipped**;红线零改动(SessionMessage/04 测试)
- [ ] **13 subagents 子代理** (`feat/13-subagents`)
  - 验收:agent 定义(frontmatter + 优先级合并 + 内建三类型 general-purpose/Explore/Plan);Agent 工具(前台嵌套 + 后台 + forkContext + worktree 隔离);SendMessage 队友通信;禁递归工具
  - 验证:嵌套调用单测(结果回收/fork 三件套/递归拒绝/前后台/失败传播/权限矩阵/worktree 隔离)
- [ ] **14 skills 技能系统** (`feat/14-skills`)
  - 验收:skill 发现/加载/契约;allowed_tools;斜杠命令机制
  - 验证:技能加载与权限联动单测
- [ ] **15 mcp MCP 客户端** (`feat/15-mcp`)
  - 验收:stdio/HTTP 传输;mcp__ 命名 + needsPermissions 强制;resources;OAuth PKCE
  - 验证:mock server 单测
- [ ] **16 bash-safety Bash 安全纵深** (`feat/16-bash-safety`)
  - 验收:破坏性守卫;LLM 意图闸门(fail-closed);沙箱计划接口(Windows 文档化降级)
  - 验证:守卫规则单测;闸门 mock 查询单测
- [ ] **17 memory 记忆系统** (`feat/17-memory`)
  - 验收:JSONL 事件溯源;保守提取(显式标记);本地词法检索;注入标注不可信
  - 验证:提取/检索单测
- [ ] **18 multimodel 多模型编排** (`feat/18-multimodel`)
  - 验收:专家模型(AskExpertModel 类);辅助回退增强;上下文感知切换(90% 预算)
  - 验证:回退链单测
- [ ] **19 plugins 热插拔注册层** (`feat/19-plugins`)
  - 验收:模块注册表;≥2 实现切换零代码侵入;插件化工具/技能/MCP 统一入口
  - 验证:切换单测 + 最终全量回归

## 收尾

- [x] V1 验收(07 完成时)✅ 2026-08-05 真实 API 通过
- [ ] 最终回归 + 项目 README(19 完成后)

## claude-code-main 借鉴任务(2026-08-05 对比新增)

> 5 代理对照 claude-code-main(38 万行)输出借鉴清单。原则:「每个错误都有恢复路径、每个恢复路径都有熔断、每个决定都幂等可重放」。

### 立即做(小改动,独立小 PR)

- [ ] **CC-01 工具契约 fail-closed**:`is_concurrency_safe` 默认 False(现默认 True,方向反了——忘了声明的新工具会并行执行);只读工具显式 True
- [ ] **CC-02 Read 去重 stub**:按 path+offset+limit 缓存已读内容+mtime,重复读返回 stub(官方数据 ~18% Read 是重复,省 token)
- [ ] **CC-03 空工具结果标记**:空 content 注入 `(toolName completed with no output)`(防 `\n\nHuman:` stop 序列误触发;现只处理 None)
- [ ] **CC-04 幂等 spill**:工具结果落盘路径按 tool_use_id 确定性生成(现每次 mkdtemp 新路径,打破 prompt cache 前缀)
- [ ] **CC-05 权限大小写归一化**:路径比较全平台强制小写(`.cLauDe` 绕过写保护,安全补丁级)
- [ ] **CC-06 symlink 双路径检查**:`resolve_candidates` 补 realpath 候选(现只查最终 resolve 一次,可穿透)
- [ ] **CC-07 session 规则接线**:loop `_permission_check` 传 `session_permissions`(现死参数,「仅本次会话允许」不可用)+ 内存态
- [ ] **CC-08 Bash 注入清单补 `=cmd`**:`=curl evil.com` 可绕过 `Bash(curl:*)` 规则
- [ ] **CC-09 斜杠命令注册表**:命令即数据对象 + 注册表(现 if/elif 链,4 个硬编码命令)
- [ ] **CC-10 错误语义化**:budget_exceeded 等用结构化结果(现 `"budget" in last_text` 字符串嗅探)
- [ ] **CC-11 优雅退出 failsafe**:幂等守卫 + failsafe 定时器 + 先 flush 会话再清理(现 13 行裸处理)

### 阶段 08(context)增强

> CC-12/13/14 已全部并入阶段 08 规格(`docs/specs/08-context.md` 3.1/3.3/3.4),随 08 实施,不再单列。

### 新阶段(建议插入路线图)

- [ ] **CC-15 错误恢复**(loop 完善):可恢复错误先扣留(413/max_output_tokens)→ 恢复阶梯(compact→升级重试)→ 防死循环闸;显式轮次状态对象 + transition reason(现任何 LLMError 直接终止,零恢复)
- [ ] **CC-16 会话 UX**:typed-entry JSONL 元数据(标题/标签与消息共存,resume 身份);粘贴引用化(paste-cache,>1KB 哈希外置);首条有意义 prompt 提取标题
- [ ] **CC-17 记忆系统(memdir 式)**:索引 MEMORY.md 常驻 + 主题文件按需取 + frontmatter 四型(user/feedback/project/reference)+ 新鲜度警示;文件方案而非 DB

### 既有阶段需求补充

- [ ] **compact(10)**:熔断器已入 08 规格(§3.5,连续 2 次失败停);10 保留:硬阻塞预留手动 /compact 空间 + boundary 消息模式
- [ ] **Bash 安全(16)**:denial-tracking(连续 3 次/累计 20 次拒绝回退人工)+ classifier 不可用 iron-gate fail-closed(照抄 CC 整套)
- [x] **hooks(09)**:钩子先于权限引擎(deny 优先/allow 短路/updatedInput 透传)+ safetyCheck bypass-免疫位 + 执行引擎管线(无钩子零开销短路/执行层去重/stdout 限额/聚合输出传递链,§4.10)—— 需求已并入 09 规格(`docs/specs/09-hooks.md` §5/§4.10)

## pi-agent-core 借鉴任务(2026-08-05 新增)

> 深读代理对照 pi/packages/agent(11081 行)输出。完整分析见 `docs/pi-agent-core-analysis.md`。pi 强在事件模型/工具管道/持久化会话/compaction;我们强在权限/审计/默认串行。最值得抄:三阶段工具管道 + 生命周期事件、结构化 compaction、树状会话。

### 归属:V1 七阶段补强(阶段 01-07 已实现模块的增强,不属后续阶段)

- [ ] **PI-01 工具执行生命周期事件**【阶段 06/07 补强】:tool_execution_start/update/end,UI 显示执行中状态与流式输出(现裸 yield 消息,拿不到工具级时序)→ engine/loop + cli/render
- [ ] **PI-02 beforeToolCall/afterToolCall 三阶段管道**【阶段 05/06 补强】:prepare(校验+权限,拒绝用 {block:true} 一等语义)→ execute → finalize(结果改写口)→ tool_queue + permissions
- [ ] **PI-03 stopReason=="length" 时 fail 全部工具调用**【阶段 02/06 补强】:防截断产生残缺参数被照常执行(现已解析完整参数的调用会执行)→ ai/client collect + engine/loop
- [ ] **PI-04 工具结果 terminate 语义**【阶段 03/06 补强】:全批同意才提前停,工具可表达「该停了」→ tools/base ToolResult + engine/loop
- [ ] **PI-06 steering/followUp 双队列**【阶段 06/07 补强】:运行中插话(steer)/结束后追问(followUp),QueueMode 控制条数 → cli/repl + engine/loop
- [ ] **PI-11 失败也走完整事件序列**【阶段 06 补强】:abort/错误路径产出完整 message→turn→agent 序列,消费者统一处理 → engine/loop
- [ ] **PI-12 Result + 稳定错误码**【阶段 03 补强】:FS/shell 期望内失败不 throw,错误码可程序化 → tools 内部约定

### 归属:后续阶段(具体阶段)

- [ ] **PI-05 结构化 compaction 管线**【已并入阶段 08,见 specs/08-context.md §3.5】:usage 优先估算 + turn 边界切割 + split-turn 前缀单独摘要 + 文件操作清单随摘要 → 新 engine/compaction.py
- [ ] **PI-07 会话操作日志 + 恢复**【阶段 12 会话生命周期】:Record(operation_started/tool_started/step_attempt)+ findOpenOperations → --continue 从中断点恢复而非消息末尾 → core/session
- [ ] **PI-08 模型/思考级别/活动工具作为 entry**【阶段 12 会话生命周期】:会话自描述,审计/恢复不用猜当时配置 → core/session
- [ ] **PI-09 树状会话**【阶段 12 会话生命周期升级,重点思想】:entry 链 + lane 指针,分支/fork 是追加指针;单文件多分支;/**tree 导航至任何先前位置继续**;按消息类型筛选;条目标记为书签 → core/session
- [ ] **PI-10 AgentMessage 分离**【阶段 12 为主(消息模型),阶段 08 辅助(context 注入边界),重点思想】:应用状态(工具记录/分支摘要/审计)与模型上下文物理分离,只在 LLM 边界 convertToLlm 转换;自定义消息类型扩展,无字符串 hack → core/messages
