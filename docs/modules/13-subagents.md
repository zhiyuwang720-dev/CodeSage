# 阶段 13:子代理(subagents)(理解文档)

> 权威设计:`docs/specs/13-subagents.md`(实现时逐字执行)。本文是设计摘要 + 决策记录 + 实现期关键裁决(S1-S7 全部交付,S8 收尾,1122 测试全绿,2026-08-15)。

## 设计摘要

子代理 = 由 Agent 工具(或编程接口)从父会话 spawn 的嵌套 AgentLoop 运行,独立上下文/工具池/会话文件,结果以 tool_result 形态回收进父对话。三个阶段能力正交叠加:定义层(S1)→ 前台嵌套(S2)→ forkContext(S3)→ 权限收窄(S4)→ 后台 + 队友通信(S5)→ 任务协作扩展(S6)→ worktree 文件系统隔离(S7)。

- **定义层(S1,§3)**:零依赖 frontmatter 解析(标量/流列表/一级 map;未知 key 忽略,畸形或无名文件静默跳过 = CC parity);`load_dir` mtime+size+digest 三键缓存;三层优先级合并 project > user > builtin;内建三类型 general-purpose / Explore / Plan。
- **Agent 工具(S2,§4/§5)**:`needs_permissions()=True` 且不进 SYSTEM_TOOLS(完整决策链 + 审计);`is_concurrency_safe=True`(同 turn 多 Agent 并行);**编译期禁递归**(Agent 从子池剔除,§4);描述动态列出全部 agents + forkContext 标注。
- **前台嵌套 run(S2,§5.4)**:子 loop 跑完回收最后 assistant text;父 abort 经 `_propagate_abort` 级联子 abort;子崩溃/超限 → 错误 tool_result 交父自愈,绝不炸父 run。
- **forkContext(S3,§5.2)**:name 缺省 = fork —— 继承父上下文,历史三件套:assistant 仅留 tool_use 块、tool_result → 占位文本(1:1 配对硬断言,防孤儿 tool_result)、末条 user = req.prompt;历史截断 FORK_MAX_MESSAGES=60。子代理会话 = sidechain 会话;父会话操作日志记 step_attempt/step_completed/step_failed 与 find_open_operations 配对。
- **权限收窄(S4,§7)**:生效模式 = min(父模式,声明模式),只收窄不放宽;ask 在子代理内自动 deny(无 UI 阻塞);fork bubble:fork 继承父 request_permission 回调 —— 权限请求冒泡到父终端。
- **后台 + Mailbox(S5,§6)**:`launch()` create_task 立即返回 async_launched;父 abort 级联 cancel;完成 → Mailbox 广播 SUBAGENT_DONE + 父 _notify 双通道;SendMessage 寻址(agent_id + address_name 双名),目标 loop 每轮迭代前 drain 注入;终态注销 inbox,目标消失后投递明确报错;L3 白名单 ASYNC_AGENT_ALLOWED_TOOLS(read/search/bash/edit/write/task 协作,排除交互元工具)。SubagentStart/SubagentStop 钩子事件(§11.2)。
- **后台完成自动注入父上下文(§6.4,实现期对齐 CC)**:后台终态(完成/失败/取消)第三通道 —— `<task-notification>` XML user 消息进父 loop `_notifications` 队列,每轮迭代前 drain 注入父 Message 流(steer/_inbox 同构);父模型下一轮自然看到结果,长时间自动化无需用户转述;跨 turn 积压,取消路径同样通知。
- **任务扩展(S6,§11)**:TaskStore `on_change` 单点回调 → TaskCreated/Updated/Completed/Deleted 事件(引擎注入 hooks dispatch wrapper,无订阅零路径);`create(owner=)` 自动归属(agent_name);claim busy check(in_progress 且 owner 非 self → 拒绝);unassign_agent 只回退非 completed;task_list_id 继承 —— 子代理与父共享同一任务列表;`_dir_lock` async 化(asyncio.to_thread)+ `_pid_alive` 回收陈旧锁。
- **worktree 隔离(S7,§5.1/§5.4)**:`isolation="worktree"` 参数或定义字段 → `git worktree add` 从 HEAD 检出独立分支,子代理 cwd = worktree(父工作区未提交变更不可见是特性);终态无变更 → 自动 `worktree remove` + 删分支;有变更 → 保留,路径追加进 result.content + metadata 回填供宿主导入;非 git 仓库显式报错不降级。

## 设计决策记录(spec 核心裁决)

1. **编译期禁递归(L1,§4)** — Agent 从子代理工具池直接剔除,模型无机会自嵌套(CC filterToolsForAgent 同款);运行期防御不需要。
2. **ask 自动 deny(§7.2)** — 子代理无终端,ask 无法呈现 → 自动 deny 转错误 tool_result 交模型自愈;后台同理(§6.1 无 UI 阻塞前提)。
3. **fork bubble(§7.3)** — 仅 fork 继承父 request_permission(权限请求冒泡到父终端);普通定义名子代理保持 None —— 避免多跳冒泡语义混乱。
4. **单文件 Mailbox 全局单例(§6.3)** — 进程内 asyncio.Queue 注册表;目标 loop 迭代前 drain 注入 = 消息进 Message 流,不另开通道。
5. **任务列表共享而非隔离(§11.1)** — 子代理继承父 task_list_id:teammate 协作同一张列表(CC IN_PROCESS_TEAMMATE 实测);owner 身份:定义名子代理用定义名,fork 用唯一 agent_id 防碰撞。
6. **worktree 清理挂 run() finally 单点(§5.4)** — 成功/异常/取消三路径一致;`git status --porcelain` 有变更才保留,状态未知(检查失败)也保留 —— 可逆优先。

## 实现期关键裁决(S1-S7,review 驱动落地)

1. **S6 bound method `is` 比较恒 False** — `store.on_change is parent._dispatch_task_event` 每次访问新建 bound method → 判属主必须用 `current.__self__ is loop`(runner 与测试两处都改,回归测试固化)。
2. **S6 Windows OpenProcess 防 pid 枚举语义** — `OpenProcess` 对不存在 pid 也可能返回 ERROR_ACCESS_DENIED(拒绝 ≠ 存活);实测 `get_last_error()==5` 无法区分 → 回退 handle 0 视为死(误回收 = 旧实现同款无回归,比卡死可接受;注释记录裁定)。
3. **S7 M1:metadata 不进父消息流** — 引擎构造 tool_result 块只带 content/is_error(loop.py 构造点),metadata 全量丢弃 → 保留 worktree 的路径必须**追加进 result.content** 才兑现「供宿主导入」契约(metadata 回填保留给编程调用方;S5 的 subagent_output 标注同属死通道,既有行为不扩改)。
4. **S7 M2:状态未知绝不强删** — `git status --porcelain` 失败(index.lock 被占/仓库损坏)时跳过检查直接 remove --force 会在最不确定的时刻销毁子代理产物 → status code!=0 一律保留。
5. **S7 M3:git 同步调用 30s 超时** — subprocess.run 无 timeout 时 Windows stale index.lock 让父事件循环同步挂死 → 超时归一化 124 按失败处置;git 未安装 FileNotFoundError 归一化 127 → WorktreeError(父模型看到「需要 git 仓库」而非裸异常)。
6. **S7 L2:is_safe_segment 终检** — slug 清洗逻辑有 bug 时静默穿越 .claude/worktrees/ → worktree_path 逐段终检,非法即拒绝。
7. **S7 L1:取消路径孤儿 worktree** — CancelledError 时 result=None 无回填渠道 → 保留场景记入父会话操作日志(step_failed + worktree 路径),不留无声孤儿。
8. **S7 工具参数 > 定义** — effectiveIsolation = req.isolation or definition.isolation;与 cwd 参数互斥(重定向语义重叠,双指歧义 → 明确 ToolError)。
9. **S8+ 后台结果自动注入父上下文(§6.4)** — 用户裁定「长时间自动化任务必然需要自动注入」,按 CC 方式落地:`_notifications` 队列 + 迭代前 drain(user 角色,steer/_inbox 同构),消息 = `<task-notification>` XML(agent_id/status/summary 200/result 全量/session_path);发出点 = `_notify_done` 三通道之一(仅后台)。与 CC 差异:CC 经 REPL 命令队列(priority later)空闲自动消费 → 模型自动继续;CodeSage 注入时机 = 下一次迭代(同 turn 后续轮次或下一用户输入),REPL 空闲自动继续属后续增强面。

## 红线固化

| 红线 | 锚点 | 状态 |
|---|---|---|
| 核心不变量零改动 | Message 流唯一通道 / 权限链 deny>ask>allow / 工具契约(扁平对象 + async gen + needs_permissions) | ✓ 全阶段回归 |
| 权限判断永远在引擎 | 工具内零权限逻辑;worktree 内决策与父工作区一致 | ✓ 权限矩阵测试 |
| Agent 禁递归 | `SUBAGENT_DISALLOWED_TOOL_NAMES = {Agent}` | ✓ 编译期池过滤测试 |
| 子代理崩溃不炸父 run | 一切异常降级错误 tool_result | ✓ 嵌套/装配失败测试 |
| 非 git 显式报错不降级 | worktree 创建失败 → WorktreeError → ToolError | ✓ 测试固化 |

## 交付与验证

- **S1**:定义层(loader 缓存/优先级/内建三类型)—— 单测绿
- **S2**:Agent 工具 + 前台嵌套 —— 结果回收/递归拒绝测试绿
- **S3**:forkContext 三件套 + sidechain + step_attempt + find_open_operations 配对 —— 纯函数单测硬断言
- **S4**:权限收窄矩阵(plan<default<yolo)+ ask 自动 deny + fork bubble —— 权限矩阵测试绿
- **S5**:后台 + Mailbox + SendMessage + hook 事件 + L3 白名单 —— 全绿
- **S6**:任务扩展(owner 自动归属/claim busy/unassign/共享列表/锁回收)—— 全绿;S6 后 1101 passed
- **S7**:worktree 隔离(21 测试:slug/创建/清理/保留/非 git/互斥/定义级/后台组合/取消清理/status 失败保留/git 缺失/content 携带路径)—— 全绿;最终 **1122 passed, 9 skipped**(2026-08-15)
- **S8**:本文档 + 主规格修订 + todo 勾选 + 合并 master
