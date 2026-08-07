# 阶段 09:Hook 系统(理解文档)

> 权威设计:`docs/specs/09-hooks.md`(实现时逐字执行)。本文是设计摘要 + 决策记录;实现完成后由实现者补充实现细节。

## 设计摘要

配置驱动的外部钩子系统:settings 三层声明钩子(事件 + matcher + command/prompt/http 执行体),八个生命周期事件(SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop / PreCompact / PostCompact / Notification)调用钩子,输出经严格校验后汇入权限决策链与消息改写。

- **结构**(镜像目录规范):`codesage/hooks/` = 契约层(types.py/base.py)+ 实现层(command.py/prompt.py/http.py)+ 入口层(registry.py)+ `_common.py`;测试镜像 `tests/hooks/test_*.py`。
- **执行**:子进程命令钩子(POSIX sh / Windows Git Bash,复用 `tools/builtin/shell/bash.py:187-200` 的 _shell_argv 模式)+ 单轮 LLM 提示钩子(quick 指针,{ok,reason} 契约)+ HTTP 钩子(httpx 既有依赖零新增;URL 白名单默认 `[]` 全禁 + SSRF 矩阵 + header 白名单插值 + CRLF 消毒,§4.9)。同步顺序执行,无异步、无 agent 钩子。
- **接线**:PreToolUse/PostToolUse 挂 tool_queue.py:161-162/167-168/202-203 的休眠 pre/post_hook 位(经 loop.py:534-539 传参接通);UserPromptSubmit 在 loop.py:160/224;Stop 在 completed 与 tool_terminated 分支;SessionStart 在 run() 首部(门闩一次);PreCompact/PostCompact 封装进 `_compact`(loop.py:205-207 auto 主路径 + :246 PTL 路径一处覆盖,PreCompact exit 2 阻止压缩、stdout 注入摘要 prompt,PostCompact 纯观察型);Notification 四 emit 位(loop.py:555-557/558/309-317、tool_queue.py:183-184)。
- **审计**:权限决策走 audit.jsonl(source=`hook:PreToolUse`,与引擎事件互斥,每工具恰好一条);钩子执行走 hooks.jsonl(每钩子一次 HookAuditEvent)。

## 设计决策记录

1. **钩子先于权限引擎** — PreToolUse 挂休眠 pre_hook 位而非改 engine.py 入口:该位已存在且恰在 `_permission_check` 之前,updatedInput 可直接改写 item.input;引擎 10 步决策链零改动。
2. **deny 优先 / allow 短路 / 无 ask** — 任一 deny 终局(后续钩子跳过);无 deny 且有 allow → 引擎不跑;无决策 → 引擎照常。ask 砍掉(需第三路 UI + explicit-approval 地板语义,todo 只要求 deny/allow)。
3. **allow 短路保留写保护地板** — CLAUDE.md 不变量「写保护路径优先于 allow」无例外;新增 `PermissionEngine.floor_check()`(仅写保护一项)守卫,命中降级为人工确认。敏感路径/工作目录/显式批准清单不做例外(它们属「默认 ask」类策略,钩子 allow 是显式授权)。
4. **fail-closed 比 CC 更严** — PreToolUse 超时 / JSON 解析失败 / spawn 失败 → deny(CC 为 non_blocking_error,工具照跑)。原则:钩子「没能说话」时安全门关闭;「说了话只是抱怨」(exit 1)才放行。
5. **免疫位(safetyCheck bypass)** — 仅同一钩子结果含 permissionDecision=allow 时生效(不能只豁免不负责);不豁免任何权限层(写保护/deny 绝对);v1 只做设置+携带+审计,消费点在阶段 16 bash-safety。
6. **updatedInput 先于引擎应用** — item.input 改写后,权限引擎与工具执行读同一改写后输入;last-wins;不落会话(重放时钩子重跑)。
7. **updatedSystemReminder 一次性 prefix** — 经 loop.py:382-400 prefix 机制注入(第三位,位置固定),只进请求不落会话;内容变化主动打破前缀缓存,属显式操作的预期代价。
8. **裁掉的能力与理由** — 22 个事件(依赖未建的 11/13/15 阶段或文件监听基建;PreCompact 的 manual trigger 归阶段 10;session_end 通知 v1 不含)、agent 钩子(依赖 13)、async/once/statusMessage(无消费场景)。HTTP 执行体、`if` 条件、PreCompact/PostCompact、Notification 经评估后已转「做」(见决策 12-15)。
9. **ToolResult.new_messages 弃用** — 无工具设置、主循环不消费;updatedInput/additionalContext 已是受审计的合规通道;保留字段防破坏构造契约,注释标记 deprecated。
10. **配置快照语义** — build_loop 解析一次,会话中改配置不生效(与 08 memoize 同哲学;权限规则仍是每工具重载,两者差异文档化)。
11. **顺序执行而非并行** — CC 并行 + 结果聚合;我们顺序(确定性审计 + 简单 fail-closed),钩子数量少时性能可忽略。
12. **compact 事件 fail-open** — PreCompact/PostCompact 接线封装进 `_compact` 内部(一处覆盖 auto 主路径 loop.py:205-207 与 PTL 路径 :246);PreCompact exit 2 是唯一显式阻塞位(本轮不压缩,防抖已占位下轮恢复),exit 0 stdout 多钩子 join 为 custom instructions 注入 `_summary_prompt`(extra_instructions 参数,compaction.py:221-227/273/289);exit 1/超时/无输出 → 压缩照常无指令。**压缩不是安全门**(与 PreToolUse fail-closed 对比):钩子失败最多损失一条压缩指令,绝不阻塞主循环;钩子失败/exit 2 不计数进熔断 `_compact_failures`。首版 trigger 恒 "auto",manual 值预留归阶段 10。
13. **通知 fail-open + 10s** — Notification 语义重定义为「系统级状态事件通知」,四源:permission_request(loop.py:555-557)/permission_denied(loop.py:558)/tool_error(tool_queue.py:183-184)/llm_error(loop.py:309-317);`HookManager.notify()` 统一 emit。通知源处于 UI 关键路径,挂起 = 权限弹窗延迟,而通知不承载任何决策——全事件 fail-open、同步、默认超时 10s。**通知不产生权限审计事件**(非决策,「每决策恰好一条」不变量不受影响);statusbar print_below 消费,无头模式仅进 hooks.jsonl。
14. **HTTP 白名单默认全禁** — 复用 httpx 零新依赖;settings 顶层 `http_hook_urls` 默认 `[]`(与 CC 的 undefined→不限**刻意分歧**):无 managed policy 层 + project settings 可入库,默认不限 = 恶意仓库可让 hook 打任意 URL;本地优先工具无远程消费方,默认锁死零成本。配套:header 白名单插值、CRLF 消毒、SSRF 矩阵(ipaddress 标准库,放行 127.0.0.1);仅 SessionStart 禁用(关键路径不依赖外部网络)。
15. **if 复用 rules.py 零解析器** — hook 级 `if?` 字段用权限规则语法 `"Tool(content)"`,直接复用 parse_rule/bash_rule_matches/path_rule_matches,新代码仅 `if_rule_matches`(~40 行);仅 PreToolUse/PostToolUse 可求值,其他事件带 if → warning + 永不执行;工具不存在/校验失败 → 恒 false;matcher 先(组级)if 后(hook 级),两层都在 spawn 前。**差异**:Bash 内容匹配是字符串前缀(CC 是 tree-sitter 语句级),规格 §2.4 如实标注。
16. **无信任门(trust gate)** — 不引入 CC 式信任对话框/trustLevel/权限文件信任门,信任语义 = 配置即信任(三层合并,local 覆盖 project 已含来源裁决)。CC 两起 RCE 教训(SessionEnd 泄露 / SubagentStop 提前执行)的触发路径 v1 不存在(无 SessionEnd / 子代理 / 对话框);「集中式检查位」对应物 = 配置解析期一次性校验 + 快照冻结(会话内不热载)。边界如实标注:首次 clone 恶意仓库 project 钩子会执行,风险由快照 + 写保护地板 + 双流审计承担(spec §4.10.7)。
17. **执行层去重** — key = `(type, command|prompt|url, if)`,匹配后 spawn 前同批去重,last-wins(配置序靠后覆盖,与 settings 分层覆盖语义一致);只执行一次、审计一次(§8.1 不变量);与配置合并层 identity-based 去重(settings.py:46-56)是**两层**,必须共存——前者管装配(同源同 JSON),后者管执行(多 matcher 组命中/跨层同内容不同实例)(spec §4.10.3)。
18. **事件→钩子数索引短路** — 配置解析期构建「事件 → 钩子数」索引,事件索引空 → 直接返回不进管线(零开销短路径,无钩子部署零侵入);过度近似设计(不查 matcher/if 只问有无配置),随快照冻结(spec §4.10.1)。
19. **聚合传递链** — additionalContext/updatedSystemReminder 多钩子输出**顺序 join('\n\n')**(对齐 PreCompact §7.4 先例);updatedInput last-wins(决策 6);prompt 钩子 `ok:false` 即阻塞信号,消费动作同 exit 2;逐事件消费总表见 spec §4.10.6。
20. **stdout 限额与解码** — stdout 捕获上限 256KB 超限截断(截断 JSON → 解析失败 → fail-closed);stderr 保持 2000 字符截断(§4.5);子进程输出 UTF-8 `errors=replace` 解码(Windows GBK 输出不抛错不中断,GBK 原文进 JSON 解析 → 校验失败,无「乱码当合法 JSON」路径)(spec §4.10.5)。
