# 阶段 09:钩子系统(Hooks)

> 参考:`.omc/artifacts/hooks-ref-claude.md`(Claude Code Hook 系统权威参考,27 事件/配置/JSON 契约/执行器/错误恢复)+ `.omc/artifacts/hooks-ref-codesage.md`(CodeSage 集成点映射)。本规格将 CC 的 Hook 系统裁剪到 CodeSage 的安全取向与最小规模。
> 代码位一律以 `codesage/codesage/` 相对路径 + file:line 锚定;「新增」= 本阶段新建的文件/位置。
> 主规格锚点:`docs/specs/codesage.md:41`(保留清单 #10:hooks 可改写权限决策与输入)、`:84`(hooks/ 目录)、`:148`(阶段 09 范围);`tasks/todo.md:102`(钩子先于权限引擎 + safetyCheck bypass-免疫位)。

## 0. 验收标准(tasks/todo.md 阶段 09 条目)

- [ ] 八事件接线:SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop / PreCompact / PostCompact / Notification
- [ ] 钩子先于权限引擎:deny 优先、allow 短路、updatedInput 透传(todo.md:102)
- [ ] safetyCheck bypass-免疫位:仅 hook allow 可设,防滥用约束,审计记录
- [ ] 命令 + 提示 + HTTP 三执行体,JSON 结果解析(command:Stdout/退出码契约;prompt:{ok,reason} 契约;http:必须 JSON),超时 fail-closed
- [ ] fail-closed:PreToolUse 钩子超时 / JSON 解析失败 → deny(不静默放行)
- [ ] 每次权限决策恰好一条审计事件(含钩子决策);钩子执行单独审计流;**通知不产生权限审计事件**
- [ ] if 条件:hook 级 `if?` 字段(权限规则语法),仅 PreToolUse/PostToolUse 可求值,工具不存在/校验失败恒 false;matcher 先(组级)if 后(hook 级),两层都在 spawn 前
- [ ] 执行引擎管线(§4.10):无钩子零开销短路(事件→钩子数索引)、执行层去重(同批只执行/审计一次)、stdout 限额(256KB 截断 + UTF-8 errors=replace)、聚合输出传递链(逐事件消费总表)
- [ ] 四个通知源 emit:permission_request / permission_denied / tool_error / llm_error;全事件 fail-open + 默认超时 10s;statusbar print_below 消费
- [ ] PreCompact/PostCompact:接线封装进 `_compact`(auto 主路径 loop.py:205-207 + PTL 路径 :246 一处覆盖);PreCompact exit 2 阻止压缩、exit 0 stdout 多钩子 join 为 custom instructions 注入摘要 prompt;fail-open;PostCompact 纯观察型
- [ ] HTTP 执行体:URL 白名单默认 `[]` 全禁、header 白名单插值 + CRLF 消毒、SSRF 矩阵、仅 SessionStart 禁用
- [ ] 现有测试全绿(515 + 09 新增);`feat/09-hooks` 分支合并 push

## 1. 目标与范围

### 1.1 做什么

为 CodeSage 实现配置驱动的外部钩子系统:settings 三层中声明钩子(事件 + matcher + command/prompt/http 执行体),引擎在八个生命周期事件上按契约调用,钩子输出经严格校验后**汇入权限决策链与消息改写**。首版形态:进程外子进程命令钩子 + 单轮 LLM 提示钩子 + HTTP 钩子,同步顺序执行。

### 1.2 不做什么(与 CC 的裁剪)

CC 共 27 事件 + 4 执行体 + 异步机制。首版裁剪:

| 裁剪项 | CC 现状 | 裁剪理由 |
|---|---|---|
| 事件 27 → 8 | PreToolUse/PostToolUse/PostToolUseFailure/Notification/UserPromptSubmit/SessionStart/SessionEnd/Stop/StopFailure/Subagent*/PreCompact/PostCompact/PermissionRequest/PermissionDenied/Setup/Elicitation*/Task*/TeammateIdle/ConfigChange/InstructionsLoaded/CwdChanged/FileChanged/Worktree*/Setup | 主规格 codesage.md:148 已同步为八事件(本行及其后三行为评估后的扩展来源);Subagent*/Task*/TeammateIdle 依赖阶段 13/11(未建);Elicitation 依赖 MCP(阶段 15);ConfigChange/InstructionsLoaded/CwdChanged/FileChanged/Worktree* 依赖配置热加载与文件监听基建;PermissionRequest/PermissionDenied 是三路竞速机制(无分类器,其通知对应物见 §2.5);Setup/SessionEnd 无对应装配需求(session_end 通知 v1 不含) |
| PreCompact/PostCompact | 压缩前可阻断/附加指令 | **已做(§2.2/§6.2/§7.4)**:首版只做 auto trigger(trigger 字段恒 "auto",manual 值预留归阶段 10);接线封装进 `_compact`(loop.py:205-207 auto 主路径 + :246 PTL 路径一处覆盖,零遗漏);PreCompact exit 2 阻止压缩、exit 0 stdout 多钩子 join 为 custom instructions 注入 `_summary_prompt`(compaction.py:221-227);**fail-open**(压缩不是安全门——与 PreToolUse 的 fail-closed 对比:权限是安全位,压缩不是;钩子失败最多损失一条压缩指令,绝不阻塞主循环);PostCompact 纯观察型。**manual trigger 仍裁**:依赖阶段 10 的手动 /compact |
| Notification | 权限弹窗/idle/auth 通知 | **已做最简档(§2.5)**:语义重定义为「系统级状态事件通知」,4 个通知源 = permission_request(loop.py:555-557)/permission_denied(loop.py:558)/tool_error(tool_queue.py:183-184)/llm_error(loop.py:309-317)。CC 的 idle/auth/elicitation 三项裁剪理由成立(CodeSage 无 idle 概念、无登录流程、无 MCP)——但只砍掉源的一部分,权限询问本就是 CC 语义继承;v1 不含 session_end |
| PostToolUseFailure | 失败独立事件 | v1 归并:失败/拒绝都以 `tool_response.is_error=true` 呈现给 PostToolUse |
| HTTP 执行体 | execHttpHook(POST + SSRF 防护 + header 白名单 + CRLF 消毒) | **已做(§4.9)**:复用既有 httpx 依赖(零新增,项目「无新依赖」约束不触发);URL 白名单默认 `[]` 全禁(与 CC 分歧:CodeSage 无 managed policy 层,project settings 可入库(§11),默认不限 = 恶意仓库可让 hook 打任意 URL;本地优先工具无远程消费方,默认锁死零成本)、header 白名单插值、CRLF 消毒、SSRF 矩阵、仅 SessionStart 禁用(关键路径不依赖外部网络) |
| Agent 执行体 | 多轮工具可用 LLM 子代理 | 依赖阶段 13 子代理机制;v1 钩子进程/提示钩子均无工具能力(见 §4.7) |
| `if` 条件 | 权限规则语法(tree-sitter 解析 Bash) | **已做(§2.4)**:复用 `permissions/rules.py` 的 parse_rule(:84)/bash_rule_matches(:153)/path_rule_matches(:61),零新增解析器、零新依赖——「需要 tree-sitter」的裁剪理由随复用方案消失。**差异如实标注**:Bash 内容匹配是字符串前缀(CC 是 tree-sitter 语句级),对 `"Bash(git *)"` 类用例语义一致,对 `&&` 复合命令与 CC 存在细微差异 |
| async/asyncRewake | 异步钩子 + AsyncHookRegistry | 异步响应轮询是独立机制;v1 全部同步,超时即失败 |
| `once` / `statusMessage` / callback / function | 执行一次即移除 / spinner 文案 / SDK 编程式 | 无 SDK 编程面;`once` 无消费场景;UI 侧留 REPL 后续阶段 |
| initialUserMessage / watchPaths | SessionStart 注入初始消息 / 文件监听 | 分别依赖未建的 FileChanged 监听与 resume 流程;v1 SessionStart 仅 additionalContext |

**不砍**:fail-closed 语义(§4.6)、deny 绝对(§5.2)、写保护地板(§5.3)、审计不变量(§8.1)。

### 1.3 与既有机制的三分法边界(对齐 W2 产物 §6)

1. **AuditSink/审计钩子**(audit.py:3-7 的「The audit hook ships from day one」)是**权限审计机制**,不是事件钩子。09 不触碰 audit.py 的契约;相反,09 的钩子决策**产出**审计事件(§8)。
2. **既有回调**(finalize loop.py:102、on_stream loop.py:99、on_tool_event loop.py:101、on_after_render repl.py:60、request_permission loop.py:85)是进程内 UI/改写回调,09 不改动、不替代。顺序关系已定:finalize → post_hook(tool_queue.py:200-203)。
3. **09 钩子** = 配置驱动的外部可编程事件源(子进程 / 单轮 LLM 调用),进程外、可审计、可 fail-closed。

## 2. 事件契约

### 2.1 HookInput 基础字段(所有事件)

```jsonc
{
  "session_id": "...",       // Session.session_id
  "cwd": "...",              // loop.cwd
  "session_path": "..."      // 会话文件路径(core/session.py:29,CC transcript_path 的对应物)
}
```

### 2.2 事件表

| 事件 | 触发时机(代码位) | HookInput 独有字段 | matcher 匹配值 |
|---|---|---|---|
| SessionStart | `run()` 入口(loop.py:154-160 之间);每次 AgentLoop 生命周期一次(门闩 `_session_started`,见 §6.2) | `source`: `"startup"` / `"resume"`(history 非空即 resume,loop.py:166);`model` | `source` |
| UserPromptSubmit | 每条用户输入:首条 loop.py:160;steer 中途输入 loop.py:224 | `prompt`(原文;updatedPrompt 改写后以改写值创建消息) | 无(忽略 matcher) |
| PreToolUse | 每工具执行前,tool_queue.py:161-162(pre_hook 位,09 填实) | `tool_name`、`tool_input`、`tool_use_id` | `tool_name` |
| PostToolUse | 工具执行后:成功路径 tool_queue.py:202-203(finalize 之后);拒绝路径 :167-168(带 denied 结果) | `tool_name`、`tool_input`、`tool_use_id`、`tool_response`(序列化 ToolResult:content + is_error) | `tool_name` |
| Stop | 模型自然停止:completed 分支 loop.py:262-278;tool_terminated 分支 :304-308 | `reason`、`last_assistant_message`(最后一条 assistant 消息的文本/块) | 无(忽略 matcher) |
| PreCompact | 压缩检查点命中:auto 主路径 loop.py:205-207(should_compact 命中、防抖置位后、`_compact` 调用前);PTL 反应式路径 :246。接线封装进 `_compact` 内部(find_cut_point 之后、generate_summary 之前),**一处覆盖两路径** | `trigger`(v1 恒 `"auto"`,manual 值预留阶段 10)、`context_tokens`(loop.py:204 `estimate_context_tokens(cleaned).tokens` 估算值,即压缩检查点的请求视图;PTL 反应式路径无检查点变量,`_compact` 内回退原始 span 估算——口径差异 cleaned vs raw)、`window`/`reserve`/`keep_recent`(CompactionConfig) | `trigger` |
| PostCompact | 压缩成功产出 summary 后:auto 主路径 loop.py:208-212、PTL 路径 :247-252;接线封装进 `_compact` 内部(成功返回前),自动覆盖两路径 | `trigger`、`compact_summary`(summary_message 构造的文本,compaction.py:328-330,含 fileOps 尾段)、`cut_index`(cut.index,被压消息数) | `trigger` |
| Notification | 四个通知源触发点:permission_request(loop.py:555-557,进入 request_permission 前)/permission_denied(loop.py:558)/tool_error(tool_queue.py:183-184,ToolError catch)/llm_error(loop.py:309-317) | `notification_type`、`message`、`title`(§2.5) | `notification_type` |

**不触发钩子的路径**(边界明示):

- 未知工具 / 输入校验失败的工具(loop.py:497-525,入队前已带 result 且 status="completed")——队列跳过非 queued 项(tool_queue.py:113),钩子不执行。
- thinking-only 恢复消息(loop.py:269-274)不是用户输入,不触发 UserPromptSubmit。
- 控制流终止(max_turns/max_budget/interrupted/error/thinking_only_exhausted)不触发 Stop 钩子——用户要退出时钩子不得把循环拉回来(§6.4 门控表)。
- 钩子自身产生的 `continue:false` 停止不再次触发 Stop 钩子(防递归)。

### 2.3 matcher 语义(对齐 CC hooks.ts:1346-1381)

三级匹配,按顺序;`matcher` 为组级字段(见 §3.1):

1. **精确/管道**:`matcher` 匹配 `^[a-zA-Z0-9_|]+$` 时,`|` 分割后逐个精确比较。`"Bash|Write"` 是 OR。
2. **正则**:含其他任何字符 → `new RegExp(matcher)`。非法正则 → 该钩子永不匹配 + 启动时 warning 日志(CC 同款 :1376-1380)。
3. **空或 `"*"`** → 匹配所有。

匹配发生在 spawn 之前,不匹配不产生进程。仅 PreToolUse/PostToolUse 按 tool_name、SessionStart 按 source;UserPromptSubmit/Stop 不匹配(带 matcher 也不生效,配置解析时警告)。

### 2.4 if 条件(二级过滤,hook 级)

hook 级可选字段 `if?: str`,语法 = 权限规则语法 `"Tool(content)"`(与 matcher 是两级:matcher 组级过滤整组,if hook 级过滤单个钩子)。复用 `permissions/rules.py` 的 `parse_rule`(:84)/`bash_rule_matches`(:153)/`path_rule_matches`(:61),**零新增解析器**;新代码仅薄封装 `if_rule_matches`(~40 行,`hooks/_common.py` 新增)。

求值规则:

1. **可求值事件:仅 PreToolUse / PostToolUse**(二者都有 tool_name + tool_input,是唯一可求值对象)。其他事件带 `if` → 配置解析 warning + 永不执行(与 §2.3 对 UserPromptSubmit/Stop 带 matcher 的处理同款,效果等价 CC 的「剔除」)。
2. **匹配语义**(基于 tools/builtin/ 实际 input schema):
   - **Bash**:`input["command"]`(required)经 `bash_rule_matches` 字符串匹配(空格归一化 + `*` 前缀通配)。**差异如实标注**:CC 用 tree-sitter 语句级解析,CodeSage 是字符串前缀匹配——对 `"Bash(git *)"` 类用例语义一致,对 `&&` 复合命令与 CC 存在细微差异(v1 覆盖度足够,ponytail 取舍);
   - **Read/Write/Edit**:`input["file_path"]`(required,缺失 → 恒 false)经 `path_rule_matches`(`/**` 递归、`/*` 单层、绝对路径前缀、fnmatch 通配全现成);
   - **LS/Glob/Grep**:`input["path"]`(可选字段,缺省 cwd;**缺失 → 恒 false**,无 path 可匹配,不猜测 cwd);
   - **其他工具**(WebFetch/Todo/Task…):无内容字段,内容规则退化为**工具级匹配**(`parsed_name == tool_name`,与权限引擎 rules.py:122 同语义)。
3. **恒 false**:工具不存在(registry.get 为 None)/ 输入校验失败(`tool.validate_input` 抛错,tools/base.py:95)/ 文件工具路径字段缺失 → 恒 false,钩子不执行。注意:base 的 validate_input 是空实现,大部分工具未覆写——「校验失败」实际以「路径字段缺失」落地,**不引入** jsonschema 校验器。
4. **求值顺序:matcher 先(组级),if 后(hook 级)**,两层都在 spawn 之前——if 不匹配:不产生子进程/HTTP 请求,不进审计事件(或记 skipped,与 §8 双流审计对齐)。对 command/prompt/http 三类执行体统一生效,无需分叉。
5. **单 `if` 字段**:CC 每 hook 只有单个 `if` 字符串字段,不存在 AND/OR 组合问题,照抄单字段;要组合就写进 matcher 正则或配置多个 hook。

### 2.5 Notification 通知事件(最简档)

**语义**:系统级状态事件通知——CodeSage 对 CC Notification 的重新定义。CC 通知源(权限弹窗/idle/auth/elicitation,coreSchemas.ts:473-482)中,权限询问是 CodeSage 唯一真实 UI 交互面;idle(单用户本地 REPL,无 idle 概念)/auth(无登录流程)/elicitation(依赖 MCP,阶段 15 未建)三项裁剪;再补 CodeSage 自己的真实事件(错误类)。**v1 不做 session_end**(run() 有 7 个终止点,统一位留给后续阶段)。

**四个通知源**(评估实测锚定):

| # | 通知源 | 触发代码位 | notification_type | 可得数据 |
|---|---|---|---|---|
| 1 | 权限询问 ask | loop.py:555-557(进入 request_permission 前) | `permission_request` | PermissionDecision(reason)、tool.name、item.input、mode |
| 2 | 权限被拒 | loop.py:558 | `permission_denied` | 同上 |
| 3 | 工具失败 | tool_queue.py:183-184(ToolError catch) | `tool_error` | tool_name、str(exc)、error_code(metadata) |
| 4 | LLM 错误 | loop.py:386-390(非流式 LLMError catch → notify) | `llm_error` | str(exc)、status_code |

**边界**:流式非 PTL provider error 走 is_error 消息路径(loop.py:314-318,completed 分支的 is_error 消息),**不 emit llm_error** —— llm_error 仅在非流式 LLMError catch(loop.py:386-390)emit。

**emit 抽象**:`HookManager.notify(notification_type, message, *, title=None, **data)`(hooks/registry.py **新增方法**),与既有事件分发同构:匹配(matcher 取 `notification_type`)→ 执行 → 审计;各触发点每点 1 行调用,不建统一总线。

**HookInput 字段**:基础三字段(session_id/cwd/session_path)+ `message`、`title`、`notification_type`(对齐 CC coreSchemas.ts:473-482)。

**语义约束**:

- **全事件 fail-open**:exit 2 与非阻塞等同(同 §4.3 PostToolUse 事件差异);超时仅日志 + 审计。理由:通知源本身处于 UI 关键路径(权限询问/错误路径),通知 hook 挂起 = 权限弹窗延迟,而它不承载任何决策——**不决策、不挂长**。v1 无 async 机制,同步但 fail-open 已足够。
- **同步执行、默认超时 10s**(比 command 60s 更短)。
- **消费端**:statusbar `print_below`(statusbar.py:124-131,经 repl.py:165-176 的 bar)——往滚动区打一行;错误类通知与既有错误渲染(provider error 已是消息,loop.py:312-316)不冲突:通知是状态行,错误是消息流。无头模式(--output-format json,repl.py:59 render=False)不渲染,通知仅进 hooks.jsonl + 日志。
- **审计**:HookAuditEvent 的 `event` 字段扩展 notification_type 值(§8.1);通知**不产生权限审计事件**(非决策,「每决策恰好一条」不变量不受影响)——**新增红线**,见 §9.2。

## 3. 配置格式

### 3.1 schema(settings.hooks)

```jsonc
{
  "hooks": {
    "PreToolUse": [                        // 事件名 → 钩子组数组
      {
        "matcher": "Bash|Write",           // 可选;组级(整组共享)
        "hooks": [                         // 同组钩子顺序执行
          { "type": "command", "command": "scripts/guard.sh", "timeout": 30 },
          { "type": "prompt", "prompt": "评估这条命令是否安全:$ARGUMENTS", "timeout": 30,
            "if": "Bash(git *)" },         // 可选;hook 级二级过滤(§2.4)
          { "type": "http", "url": "http://127.0.0.1:8000/guard",
            "headers": { "Authorization": "Bearer $TOKEN" },
            "allowedEnvVars": ["TOKEN"] }  // 仅 http;header 插值白名单(§4.9)
        ]
      }
    ],
    "SessionStart": [],
    "UserPromptSubmit": [],
    "PostToolUse": [],
    "Stop": [],
    "PreCompact": [],
    "PostCompact": [],
    "Notification": []
  },
  "http_hook_urls": []                     // settings 顶层:HTTP 钩子 URL 白名单,默认 [] 全禁(§4.9)
}
```

钩子字段(zod 式校验,实现于 `hooks/types.py` **新增**):

| 字段 | command | prompt | 说明 |
|---|---|---|---|
| type | `'command'` 必填 | `'prompt'` 必填 | 互斥;另有 `'http'`(§4.9) |
| if | 可选 | 可选 | hook 级二级过滤,权限规则语法 `"Tool(content)"`;仅 PreToolUse/PostToolUse 可求值(§2.4);其他事件带 if → 配置解析 warning + 永不执行 |
| command / prompt / url | `command: string` 必填 | `prompt: string` 必填 | command 是 shell 命令字符串(可执行文件路径 + 参数、`python -c "..."` 均可,由 shell 解释);url 仅 http:目标 URL,必须命中 settings 顶层 `http_hook_urls` 白名单(默认 `[]` 全禁) |
| timeout | 秒,正数,默认 **60** | 秒,正数,默认 **30** | 见 §4.2 默认值理由;http 默认 **60**(§4.9) |
| model | — | 可选,默认 `"quick"` 指针 | 走 ai/client.py 指针解析,失败自动回退 main(既有机制) |
| headers / allowedEnvVars | — | — | 仅 http:`headers` 值 `$VAR`/`${VAR}` 插值仅替换 `allowedEnvVars` 白名单内变量,未列入 → 空字符串(防 $HOME/$AWS_SECRET_ACCESS_KEY 泄漏);插值后 CRLF 消毒(§4.9) |

未知事件名 / 未知钩子字段 / 非法 timeout:该条**丢弃 + warning 日志**(含错误详情)。`hooks` 非 dict → error 日志,视为无钩子。

### 3.2 三层合并与快照语义

- **合并**:settings 三层 user < project < local(SettingsStore,settings.py:76-91)。`_deep_merge` dict 递归 + list 拼接去重(settings.py:33-56;注释已明言「Lists of dicts (e.g. hooks) fall back to identity-based dedup」)。同事件数组跨层拼接:user 在前、project 次之、local 在后;**执行顺序 = 合并后数组顺序**。同源同 JSON 的钩子配置去重(settings.py:46-56 既有逻辑,09 不新增去重)。
- **解析位置**(决策):`hooks/registry.py`(入口层)的 `load_hook_manager(hooks_cfg, *, client, audit)` **新增**——配置解析放 hooks 模块而非 config 层,因为解析产出的是执行器对象(command/prompt 双类),config 层不依赖 hooks 类型。
- **快照语义**(对齐 CC §2.3 与 08 memoize):`build_loop`(assemble.py:43 已 load_settings)解析一次,会话中 settings.json 修改不生效。与权限规则的既有加载方式(loop.py:550 每工具重载)不同——hooks 是快照,文档化此差异:钩子含命令,热重载的语义边界更危险。
- `Settings.hooks` 字段(settings.py:29)已存在且 test_settings.py:92-97 断言透传——09 只消费,不改字段定义。

## 4. 执行体规范与执行引擎

### 4.1 执行形态

- command 是 shell 命令字符串。POSIX 用 `/bin/sh -c`;Windows 优先 Git Bash(`shutil.which("bash")`),否则平台默认——复用 `tools/builtin/shell/bash.py:187-200` 的 `_shell_argv` 模式(**复用现成逻辑**,不新写路径归一化;Windows 盘符路径归一化既有修正已在 bash 工具内,钩子命令由 shell 处理)。
- stdin:写入 HookInput JSON + 换行(CC :1210 同款);stdin 提前关闭视为错误。
- 环境:继承进程环境 + `CODESAGE_PROJECT_DIR`(= loop.cwd)。不做 `$VAR` 占位符替换(v1 无插件系统)。
- `$ARGUMENTS` 占位符(prompt 钩子):替换为 HookInput JSON(CC hookHelpers.ts:30-35 的最小版;不做 `$ARGUMENTS[n]` 索引)。

### 4.2 默认超时

| 执行体 | CodeSage 默认 | CC 默认 | 理由 |
|---|---|---|---|
| command | **60s** | 10 分钟(hooks.ts:166) | 钩子位于工具执行关键路径:挂起钩子拖住整个循环的代价 >> 一次合法长任务的收益;逐钩子可配 timeout |
| prompt | **30s** | 30s(execPromptHook.ts:55) | 同 CC |
| http | **60s** | 10 分钟 | 对齐 command 理由(§4.9);CC 为 10 分钟,CodeSage 更短更安全 |
| 通知事件(§2.5) | **10s**(覆盖执行体默认) | — | 通知源处于 UI 关键路径(权限弹窗/错误路径),挂起 = 权限弹窗延迟,而通知不承载任何决策——不决策、不挂长;逐钩子 timeout 仍可覆盖 |

超时实现:asyncio.wait_for 包裹子进程;超时 → 杀进程,按 §4.6 fail-closed 处理。

### 4.3 stdout 解析与退出码(CC §3.3/3.4 裁剪版)

**stdout**:不以 `{` 开头 → 不解析,plainText(仅日志);以 `{` 开头 → JSON 解析 + schema 校验,失败按 §4.6。HTTP 执行体**必须**返回 JSON(§4.9:空 body → `{}` 成功,非空非法 → fail-closed)。捕获限额(256KB 截断)与解码策略见 §4.10.5。

**退出码语义**:

| 退出码 | 含义 | 效果 |
|---|---|---|
| 0 | 成功 | stdout 按 §4.4 解析;plainText 仅进调试日志(CC 的 transcript 注入我们不做——CodeSage 无 transcript 钩子显示层) |
| 2 | 阻塞错误 | stderr 构造 blockingError:PreToolUse → 阻止工具(deny);UserPromptSubmit → 阻止提交(输入丢弃);Stop → 阻止停止(对话继续) |
| 1 或其他 | 非阻塞错误 | stderr 显示给用户 + 日志,流程继续(CC :2682-2697 同款) |

**事件差异**(对齐 CC hooksConfigManager 每事件描述):
- SessionStart:exit 2 **忽略**(会话已开始,阻塞无意义,CC :86-94 同款);exit 0 的 stdout 视为 plainText。
- UserPromptSubmit:exit 2 阻止处理并擦除原始 prompt(CC :81-85)。
- Stop:exit 0 stdout 不显示;exit 2 stderr 传模型并继续对话(CC :95-99)。
- PostToolUse:exit 2 与非阻塞等同(观察型事件,无阻塞位)。

### 4.4 JSON 输出契约(HookJSONOutput)

```jsonc
{
  // ---- 通用 ----
  "continue": false,               // Stop: false → 停止,stopReason 作为停止原因
  "stopReason": "完成",             // Stop: continue=false 时的停止原因(即 stopReasonOverride)
  "decision": "approve|block",     // 兼容别名(CC 兼容层):approve→allow,block→deny;仅 PreToolUse 有意义
  "systemMessage": "...",          // 显示给用户的警告消息
  "suppressOutput": true,          // 接受但惰性(plainText 本就不落 transcript)
  // ---- PreToolUse ----
  "permissionDecision": "allow|deny",   // 优先级高于顶层 decision
  "permissionDecisionReason": "...",
  "updatedInput": { "...": "改写后的输入" },
  "immune": true,                  // 仅与 permissionDecision=allow 同结果时生效(§5.5)
  // ---- UserPromptSubmit ----
  "updatedPrompt": "...",          // 替换提交的 prompt 文本(CodeSage 新增,CC 无此字段;与 updatedInput 对称)
  "updatedSystemReminder": "...",  // 下一次请求的一次性 reminder 前缀(§7.2)
  // ---- SessionStart / UserPromptSubmit ----
  "additionalContext": "...",      // 注入为一次性 reminder 段(§7.1)
  // ---- 扩展位 ----
  "hookSpecificOutput": null       // v1 恒 null,保留字段;未来事件新增字段放这里(CC 的 per-event union 结构)
}
```

校验规则(实现于 `hooks/types.py`):
- 字段名/类型逐项校验;未知字段 → 校验失败(拒绝整个输出,CC 同款 fail-closed)。
- `permissionDecision` 枚举限 `"allow"|"deny"`(v1 **不做 `"ask"`**,见 §5.2);`immune: true` 且同结果无 allow → 免疫位忽略 + validation 警告。
- 事件不匹配的字段(如 Stop 事件带 permissionDecision)→ 校验失败(CC :583-590 同款,事件名校验是安全位)。
- `async`/`asyncTimeout` 不在 v1 schema,出现即校验失败。

### 4.5 stderr 处理

stderr 全文捕获:exit 2 → blockingError 内容;exit 1/其他/超时 → 显示给用户 + 日志。内容长度裁剪(>2000 字符截断)防超大输出;stdout 捕获限额与解码策略见 §4.10.5。

### 4.6 fail-closed 语义(与 CC 的刻意分歧)

| 失败类型 | CC 行为 | CodeSage v1(安全取向) | 理由 |
|---|---|---|---|
| JSON 解析/校验失败(stdout 以 `{` 开头) | non_blocking_error,工具照常执行(CC §6.1 表) | **PreToolUse → deny**(工具不执行);其他事件 → 非阻塞错误 | 钩子是安全信号源:输出不可解析即无法证明安全,不得静默放行(安全语义不砍) |
| 超时 | cancelled,非阻塞(CC :1300-1308) | **PreToolUse → deny**;其他事件 → 非阻塞错误 | 超时 = 钩子未能给出裁决;安全门前的挂起必须关门,不关门就是静默绕过 |
| spawn 失败(命令不存在等) | non_blocking_error(CC :2698-2730) | 同左;PreToolUse 下**等同校验失败 → deny** | 同上 |
| exit 1/其他 | fail-open:仅显示,流程继续 | 同 CC(显式退出 1 是钩子作者自声明「我没事」,与无法运行不同) | 尊重钩子作者的显式语义 |

**原则一句话**:钩子「没能说话」时安全门关闭(deny);钩子「说了话但只是抱怨」时(exit 1)流程继续。 

> **注(09 实现定案)**:shell 中介下 `exit 127` = 命令不存在 —— 按 spawn 失败处理
> (fail-closed,PreToolUse → deny);钩子脚本显式 `exit 127` 一并同判(与 CC 非阻塞
> 取向刻意分歧,安全取向优先;由 command.py run() 在退出码分类前拦截)。

### 4.7 钩子内权限(不做 agent 钩子)

- command 钩子 = 纯子进程,**无工具能力**(与 CC execCommandHook 一致)。
- prompt 钩子 = 单轮 LLM 调用,**无工具能力**(CC execPromptHook 同款:tools 不传 / toolChoice undefined)。
- agent 钩子(多轮、可用工具、dontAsk 权限)v1 **不做**——它需要阶段 13 子代理基建,且「钩子调用工具」的权限语义要单独设计(CC 用 mode=dontAsk + 工具黑名单 ALL_AGENT_DISALLOWED_TOOLS,execAgentHook.ts:98-105)。

### 4.8 prompt 执行体输出契约

- **执行形态**:单轮 LLM 调用(LLMClient;`model` 默认 `"quick"` 指针,失败自动回退 main,既有机制);无工具能力(§4.7);`$ARGUMENTS` 占位符替换为 HookInput JSON(§4.1);默认超时 30s(对齐 CC execPromptHook.ts:55)。
- **强制 JSON 输出**:请求以 json_schema 强制 `{ok: bool, reason: str}`(参考 CC execPromptHook 的 `{ok, reason}` outputFormat,hooks-ref-claude.md §5.1)。**落地差异(S10 回写)**:ai/ 无 json_schema/response_format 通道,「强制 JSON」以**系统提示强制 JSON + 客户端严格校验**(prompt.py SYSTEM_PROMPT/parse_hook_output,含 unknown field → additionalProperties:false 语义)落地——安全语义不缩水,仅缺 provider 侧 schema 强制。输出不可解析或字段缺失 → 按 §4.6 表 fail-closed:PreToolUse → deny;其他事件 → 非阻塞错误;**Stop → 放行 + warning 日志**(与 §4.6 表「其他事件 → 非阻塞错误」一致,Stop 无安全门,挂起不得把对话困住)。
- **ok:false 语义按事件区分**(prompt 钩子无退出码,ok:false 即阻塞信号):
  - **PreToolUse → deny 决策**:拒绝 ToolResult(`Permission denied by hook {name}: {reason}`,error_code=permission_blocked);reason 经 `decision.reason` 进审计(§8.1 的 ToolAuditEvent.reason,source=`hook:PreToolUse`)且用户可见。
  - **Stop → 阻止停止**(等同 `continue:false`):停止被拦下,reason 注入 feedback 消息让模型继续(§6.4 exit 2 同路径)。
  - **UserPromptSubmit → 阻止提交**(等同 exit 2):输入丢弃 + 阻塞文本显示。
  - SessionStart / PostToolUse → 非阻塞(观察型,同 §4.3 事件差异表)。
- **ok:true → 无决策**:流程继续(PreToolUse 下引擎照常求值)。

### 4.9 HTTP 执行体(hooks/http.py **新增**,复用既有 httpx,零新依赖)

- **配置**:`{ "type": "http", "url": "...", "headers": {...}, "allowedEnvVars": [...] }`。type 字段判别,无需探测 command/url。
- **请求格式**:POST,body = HookInput JSON,`Content-Type: application/json`,`max_redirects=0`。`$ARGUMENTS` 占位符不适用(HTTP body 即输入)。
- **响应契约**:与 command 同契约(HookJSONOutput,types.py 复用),但 **HTTP 必须返回 JSON**——空 body → `{}` 成功;非空且不以 `{` 开头或非法 JSON → 校验失败,按 §4.6 表 fail-closed(PreToolUse → deny)。
- **超时**:默认 **60s**(对齐 command §4.2 理由;CC 为 10 分钟,CodeSage 更短更安全),asyncio.wait_for 实现。
- **错误处理**:非 2xx / 网络错误 / 超时 → 按 §4.6 表:PreToolUse → deny;其他事件 → 非阻塞错误。
- **执行体隔离**:hooks/http.py 自建 `httpx.AsyncClient(timeout=...)`,每请求一次或模块级复用均可;**不**共享 LLMClient 的 client 实例(带 VCR transport + 300s read 超时,语义不符)。TLS 证书验证走 httpx 默认(不动);代理走 httpx 默认 `trust_env=True`(读 HTTP_PROXY/HTTPS_PROXY/NO_PROXY)——无 CC 的 sandbox 强制代理层,文档化差异。

**安全约束(与 CC 的分歧处标注)**:

| 约束 | 规则 | 与 CC 的关系 |
|---|---|---|
| **URL 白名单** | settings 顶层新增 `http_hook_urls: list[str]`(Settings 类 extra=allow,无需改 schema);`*` 通配匹配(对齐 CC execHttpHook.ts:137-145);URL 未命中白名单 → 钩子不执行 + 配置解析 warning。**默认 `[]` = 全禁** | **刻意分歧**:CC undefined → 不限。理由:CodeSage 无 managed policy 层,project settings 可入库(§11 信任注入点),默认不限 = 恶意仓库可让 hook 打任意 URL;本地优先工具无远程消费方,默认锁死零成本。放行 + 默认白名单空 = 双保险 |
| **header 白名单** | `headers` 值 `$VAR`/`${VAR}` 插值仅替换 `allowedEnvVars` 白名单内变量,未列入 → 空字符串 | 照抄 CC(防 $HOME/$AWS_SECRET_ACCESS_KEY 泄漏) |
| **CRLF 消毒** | 插值后剥离 `\r\n\x00` | 照抄 CC |
| **SSRF** | ipaddress 标准库实现:禁 0/8、10/8、100.64/10(CGNAT/云元数据)、169.254/16、172.16/12、192.168/16、IPv6 fc00::/7、fe80::/10、`::`;**放行 loopback 127.0.0.1**(本地策略服务器是真实场景) | 照抄 CC ssrfGuard 逻辑 |

**事件适配——仅 SessionStart 禁用**:SessionStart 位于 run() 入口同步 await 位(§6.2),会话启动是**关键路径**,不应依赖任何外部网络;其余事件(UserPromptSubmit/PreToolUse/PostToolUse/Stop/PreCompact/PostCompact/Notification)均在既有 await 点,与 command 执行体等位,允许。Setup 事件 09 未建,天然不存在。

### 4.10 执行引擎流水线(统一管线)

> 对齐 CC 7.4 六 Stage 执行引擎(精读产物 `.omc/artifacts/exec-engine-ref.md`,Stage 定义 ref §7.4:404-414)。本小节是执行管线唯一权威描述:§4.1-§4.9 的执行体契约经它调度,§2 的匹配规则经它消费;与既有章节的衔接逐小节标注,规则本身不重复。

**流水线总序**(一次事件触发的完整链路,CC 流程图 ref §7.4:404-414 的顺序模型裁剪版):

```
事件触发(统一入口 HookManager;abort 检查,§6.3)
  → ⓪ 快速存在性检查(§4.10.1):事件索引为空 → 直接返回,零开销
  → ① 输入构建(§4.10.4):HookInput 组装 + 惰性 JSON 序列化一次,同批共享
  → ② 匹配与 if 求值(§4.10.2):matcher 组级(§2.3)→ if hook 级(§2.4),均在 spawn 前
  → ③ 执行层去重(§4.10.3):(type, command|prompt|url, if) 同批只保留一次
  → ④ 顺序执行(§4.10.4):逐钩子 spawn,超时按 §4.2;批次中 abort → 跳过剩余(§6.3)
  → ⑤ 输出解析(§4.10.5):HookExitCode 分类 + stdout 限额 + JSON/plainText 分支(§4.3)
  → ⑥ 结果聚合与输出传递(§4.10.6):决策合并(§5.2)/消息改写(§7.1)/Stop 门控(§6.4)
  → ⑦ 审计(§4.10.6,§8):权限流 audit.jsonl + 执行流 hooks.jsonl,每钩子恰好一条
```

**与 CC 的刻意分歧总表**(细则在各小节):顺序执行替代并行 + 流式聚合(§6.3 既有决策);fail-closed 严于 CC(§4.6 既有);无 async/asyncRewake(§2.5 既有);执行层去重 key 无 pluginRoot/skillRoot 维度(§4.10.3);additionalContext 多钩子 join 而非 last-wins 覆盖(§4.10.6);无信任门(§4.10.7)。

**4.10.1 快速存在性检查(Stage 0)**

**设计决策**:HookManager 持「事件 → 钩子数」索引,构建于配置解析期(load_hook_manager 时,与快照冻结同步);事件触发先查索引,计数 0 → 直接返回,不进管线。这是零开销短路径,同时是「未配置钩子的常规路径零侵入」保证——无钩子部署下每次事件仅一次 dict 查找。

- **过度近似设计**(照抄 CC `hasHookForEvent`,ref §7.4:416-434):只问「该事件是否配置了任何钩子」,不检查 matcher/if 是否可能命中。假阳性只多走一次完整匹配路径;假阴性会漏执行应跑的钩子,宁可多查不可漏查。
- **索引来源** = settings 三层合并后的钩子配置(§3.2);CC 的 SDK/插件/会话钩子来源 v1 裁剪(§1.2),索引即全量。
- **随快照冻结**(§3.2):索引在解析期构建后不再更新,会话内 settings.json 修改不生效。
- 与 §9.1 测试:test_manager 补「无配置事件不 spawn」断言(索引空 → 零路径)。

**4.10.2 匹配与收集(Stage 2,引用不重复)**

**设计决策**:本阶段规则全部沿用既有设计,本小节只定收集顺序与边界:
- **收集顺序** = settings 合并后数组顺序(§3.2:user 在前、project 次之、local 在后)。
- **matcher 三级**(§2.3)+ **hook 级 if**(§2.4):matcher 组级先、if hook 级后,两层都在 spawn 前;if 不匹配 → 不 spawn、不进审计(或记 skipped,与 §8 双流审计对齐)。
- **每事件匹配值**取法 = §2.2 事件表(matcher 匹配值列);HTTP 事件限制 = §4.9(仅 SessionStart 禁用)。
- **与 CC 关系**:matchQuery switch(CC ref §7.4:471-500)照抄,由 §2.2 事件表承担;来源枚举裁剪为 settings 三层(CC 的注册/会话/函数钩子 v1 不存在)。

**4.10.3 执行层去重(Stage 3)**

**设计决策**:执行层去重——位置 = 匹配收集后、spawn 前,同一事件批次内;key = `(type, command|prompt|url, if)` 序列化;key 冲突保留配置序靠后者(last-wins,与 settings 分层覆盖语义一致:后合并的 project/local 覆盖先声明的 user)。**去重后只执行一次、审计一次**——被去重的钩子不产生额外审计事件,§8.1「每次钩子调用恰好一条 HookAuditEvent」不变量由此保持。

- **与配置合并层去重的两层划界**(关键):§3.2 的去重是 settings 三层合并时的 identity-based 去重(settings.py:46-56,装配期,同对象/同 JSON 的 dict 不重复入数组)——那是配置层,防「同源同 JSON 重复声明」;本小节是执行层,防「同一钩子被多个 matcher 组命中(§2.3 matcher 为组级,两组可含同一 command)或跨层重复声明(合并层去重作用于组元素级,组内钩子跨组重复不命中)」。两层必须共存:前者管装配,后者管执行。
- **若不做**:同一 command 多组命中 → 执行两次(副作用翻倍)+ 两条审计事件(不变量自违)+ §5.2 决策合并中 allow 被重复计数。
- **与 CC 关系**:CC key = `{pluginRoot ?? skillRoot ?? ''}\0{payload}`(ref §7.4:508-513);CodeSage 无插件/技能来源(v1),来源根维度裁剪(阶段 19 插件期在 key 前加来源前缀即可);「不同 if 不去重」语义等价保留(if 在 key 内:命令相同 if 不同 = 不同钩子,§2.4 单 if 字段)。

**4.10.4 输入构建与变量替换(Stage 4)**

**设计决策**:统一输入构建入口(HookManager 内、spawn 前)——基础字段(§2.1:session_id/cwd/session_path)+ 每事件独有字段(§2.2)组装为 HookInput;**惰性 JSON 序列化**(照抄 CC `getJsonInput` 闭包,ref §7.4:542-556):同批次全部钩子共享一次 `json.dumps` 结果(5 个钩子一次事件只 stringify 一次;序列化失败只报一次错)。

**stdin/body 构造与变量替换(三类执行体集中表,替换规则按 ref §7.4:525-556 数值)**:

| 执行体 | 输入通道 | 变量替换规则 |
|---|---|---|
| command | stdin = HookInput JSON + 换行(§4.1) | 环境继承 + `CODESAGE_PROJECT_DIR`(§4.1);**不做 `$VAR` 替换**(v1 无插件系统) |
| prompt | 模板替换 | `$ARGUMENTS` → HookInput JSON(CC hookHelpers.ts:30-35 最小版;不做 `$ARGUMENTS[n]`/`$0` 索引,刻意裁剪) |
| http | body = HookInput JSON(§4.9) | header 值 `$VAR`/`${VAR}` 插值仅限 `allowedEnvVars` 白名单(§4.9);body 不做替换 |

- session_id / cwd 等字段经 HookInput JSON 统一传递(三类执行体一致,不另设占位符)。
- **顺序执行 + 超时 + abort**(引用 §6.3):逐钩子 spawn、各自超时(§4.2 默认表 + 逐钩子 timeout 覆盖);批次中 abort 置位 → 跳过剩余钩子,不产生决策(§6.3)。**批次阻塞上限 = Σ 各钩子 timeout**(顺序模型固有代价:PreToolUse 批次内 N 个 60s 钩子 = 工具最多被拖 N 分钟——已在 §4.2 默认值收紧中权衡,v1 钩子数量少,配置作者自担;与 CC 并行的「最慢钩子」对比如实标注)。abort 不杀运行中子进程(§11 Windows 边界,仅超时杀)。

**4.10.5 输出解析:HookExitCode 分类 + stdout 限额(Stage 5)**

**设计决策**:统一解析入口(HookManager 内)——stdout 分支(§4.3):不以 `{` 开头 → plainText(仅日志);以 `{` 开头 → JSON 解析 + schema 校验(§4.4),失败按 §4.6 fail-closed。**HookExitCode 分类:0 = 成功 / 2 = 阻塞错误 / 1 及其他 = 非阻塞**(§4.3 表同构,此处统一命名;CC HookExitCode 语义 ref §7.4:600-615)。事件差异(§4.3)与 fail-closed 表(§4.6)继续适用。

- **stdout 捕获限额(新增,补 §4.3/§4.5 缺失)**:捕获上限 **256KB**,超限截断(保留前 256KB + 截断标记入日志)。截断语义:JSON 被截断 → 解析失败 → 按 §4.6 fail-closed(PreToolUse deny);plainText 截断 → 仅日志。与 stderr 截断(>2000 字符,§4.5)并列——stderr 是给人看的摘要、stdout 是给机器解析的契约,尺度不同,方向一致(防钩子灌输出)。
- **解码策略(新增)**:子进程 stdout/stderr 按 UTF-8 解码,`errors=replace`——Windows GBK 输出 → 替换符,不抛错不中断;GBK 原文进 JSON 解析 → 校验失败 → fail-closed(不存在「乱码被当合法 JSON」路径)。与 §4.1 Git Bash 选择同章配套。
- **validationError 附期望 schema**(照抄 CC DX 细节,ref §7.4:643):校验失败错误信息含完整期望 schema(hooks/types.py 渲染),钩子作者不用查文档。

**4.10.6 结果聚合与输出传递(Stage 6)**

**设计决策**:顺序模型下逐钩子聚合(CC 的流式 `for await ... of all()` 裁剪——「阻塞错误立即传播」由「首个 deny 短路」等价实现,§5.2)。聚合产物 = 逐事件消费,总表如下(合并 §4.3 事件差异 / §4.8 ok:false 按事件 / §6.4 Stop 结果三处散表):

| 事件 | exit 2(blockingError) | continue:false + stopReason | permissionDecision | 消息改写通道 |
|---|---|---|---|---|
| SessionStart | 忽略(§4.3) | — | 字段校验失败(仅 PreToolUse 有意义,§4.4) | additionalContext 多钩子 join('\n\n') 注入一次性 reminder(§7.1) |
| UserPromptSubmit | 阻止提交,输入擦除(§4.3) | — | 同上 | updatedPrompt 替换文本(§7.1);updatedSystemReminder/additionalContext 多钩子 join('\n\n') 为一次性 prefix(§7.2) |
| PreToolUse | deny(§4.6) | — | §5.2 合并:deny 终局短路 / allow 短路(引擎不跑)/ 无决策引擎照常;`ask` 不做 | updatedInput last-wins(§5.4;deny 钩子改写不生效) |
| PostToolUse | 非阻塞等同(§4.3) | — | 字段校验失败 | 无改写通道(§7.1 无 PostToolUse 行) |
| Stop | stderr 注入 feedback 消息,继续循环(§6.4) | `_stop("hook", stopReason)`(§6.4) | 字段校验失败 | 无 |
| PreCompact | 阻止本轮压缩(§6.2) | — | 字段校验失败 | exit 0 stdout 多钩子 join('\n\n') 注入摘要 prompt(§7.4) |
| PostCompact | 非阻塞等同(§4.3) | — | 字段校验失败 | 无(纯观察型) |
| Notification | 非阻塞等同(§2.5) | — | 字段校验失败 | 无(statusbar 状态行,§2.5) |

- **多钩子上下文合并语义(新增决策)**:additionalContext / updatedSystemReminder 多钩子输出**顺序 join('\n\n')**,对齐 PreCompact custom instructions 先例(§7.4);updatedInput 多钩子 last-wins(§5.4 既有,本表引用不重复)。
- **prompt 执行体无退出码**:`ok:false` 即阻塞信号,消费动作同本表 exit 2 行(§4.8)。
- **审计发射**(§8):权限流(audit.jsonl,决策时 emit)+ 执行流(hooks.jsonl,每钩子调用恰好一条);执行层去重后只审计一次(§4.10.3)。
- **与 CC 关系**:AggregatedHookResult 字段映射——blockingError ≈ exit 2;preventContinuation ≈ `continue:false`;stopReason ≈ stopReasonOverride;permissionBehavior 的 allow/deny ≈ §5.2;`ask` 裁剪(§5.2);updatedMCPToolOutput 不适用(§4.4 hookSpecificOutput 保留位);事件发射(emitHookStarted/Response/Progress)裁剪为 §8 审计日志(无 spinner/远程显示层)。

**4.10.7 信任决策:无信任门(Stage 1)**

**设计决策**:**CodeSage 不引入 CC 式信任对话框 / trustLevel / 权限文件信任门**。信任语义 = **配置即信任**:settings 三层合并(local 显式 opt-in 手写、project 可入库),来源裁决由合并顺序承担(settings.py:19 local 覆盖 project)。

理由(对照 CC 两起 RCE 漏洞教训,ref §7.4:450-459):
1. **CC 信任门是被漏洞逼出来的**:SessionEnd Hook 泄露(用户拒绝信任后退出,SessionEnd 钩子退出时不查信任照跑,ref §7.4:454)与 SubagentStop 提前执行(子 Agent 在信任对话框弹出前完成,事件落在未信任工作区,ref §7.4:455)。CodeSage 无对话框机制、无 SessionEnd 事件、无子代理(§1.2 裁剪)——两起漏洞的触发路径在 v1 不存在,信任门要防御的场景已随机制裁剪消失。
2. **「集中式检查位」原则照抄**(CC 源码注释 "This centralized check prevents RCE vulnerabilities for all current and future hooks",ref §7.4:459)——CodeSage 的对应物 = **配置解析期一次性校验 + 快照冻结**:load_hook_manager 解析时完成字段校验 / URL 白名单 / header 白名单 / 非法正则等全部检查(§3.1/§3.2),会话中配置不热载(§3.2 快照语义);恶意仓库 clone 后即使改写 settings.json,当前会话不生效,下次启动由解析期校验兜底。
3. **既有缓解构成安全网**(§11 首条风险):写保护地板不可突破(§5.3)、双流审计可追溯(§8)、local 覆盖 project 的来源裁决(settings.py:19)。

**边界(与 CC 的刻意分歧,如实标注)**:无「无信任 → 全部跳过」门——首次运行恶意仓库的 project 层钩子会执行,与 CC 拒绝信任后跳过相反。v1 接受此边界:单用户本地工具,配置作者 = 用户本人(CC 面向多人/远程工作区);代价由快照 + 地板 + 审计承担,而非执行前信任判定。若未来出现远程/共享工作区场景,信任门可在本小节预留的集中检查位处补入(解析期校验 + 快照冻结已集中,无需散落各事件)。

## 5. 权限引擎集成(核心,todo.md:102)

### 5.1 位置:钩子先于权限引擎

**决策**:PreToolUse 钩子挂 tool_queue.py:161-162 的休眠 pre_hook 位(经 loop.py:534-539 传参接通),**不**改 engine.py 入口。

理由:
1. pre_hook 位已存在且恰在 `_permission_check`(tool_queue.py:163)之前——「钩子先于权限引擎」零新增管线;
2. pre_hook 有 `item` 引用,updatedInput 可直接改写 `item.input`(引擎与执行读同一字段);
3. 引擎 10 步决策链(tool_queue.py 之前的 engine.py:81-163)**一行不改**——钩子是引擎外一层包装,「权限判断永远在引擎」不变量保持(钩子层只做决策合并,不做权限求值);
4. deny 结果复用 `permission_blocked` error_code(tool_queue.py:166)→ 自动获得 sibling 豁免(工具被拒不株连同批,tool_queue.py:127-135)。

**队列流程改造**(tool_queue.py:156-169,09 改动):

```python
async def _execute(self, item: ScheduledTool) -> ToolResult:
    if item.context.abort_event is not None and item.context.abort_event.is_set():
        return ToolResult("(interrupted by user)", is_error=True)
    item.status = "executing"
    if self._pre_hook is not None:
        denied = await self._pre_hook(item)          # 钩子层:决策合并 + updatedInput/immune 落位
        if denied is not None:                        # 钩子 deny → 直接拒绝
            denied.metadata.setdefault("error_code", "permission_blocked")
            if self._post_hook is not None:
                await self._post_hook(item, denied)
            return denied
    if not item.hook_allowed and self._permission_check is not None:   # allow 短路:跳过引擎
        denied = await self._permission_check(item)
        if denied is not None:
            denied.metadata.setdefault("error_code", "permission_blocked")
            if self._post_hook is not None:
                await self._post_hook(item, denied)
            return denied
    # ... 原有执行/finalize/post_hook 不变
```

ScheduledTool(tool_queue.py:57-66)**新增**两个字段:`hook_allowed: bool = False`、`immune: bool = False`。休眠 pre_hook 签名(tool_queue.py:77)由 `async (ScheduledTool) -> None` 定形为 `async (ScheduledTool) -> ToolResult | None`(拒绝返回 ToolResult,否则 None)。

### 5.2 决策合并(deny 优先 / 无 ask)

**v1 不做钩子 `ask`**:permissionDecision 枚举仅 `allow|deny`。理由:ask 需要把钩子消息路由进 request_permission UI 流程(loop.py:85)并处理 requires_explicit_approval 地板,yolo 不可绕过(engine.py:154-158)——第三路 UI 是角力场景;todo.md:102 只要求 deny 优先/allow 短路。需要时后续补 `"ask"` 值,合并优先级 CC 已给:deny > ask > allow(hooks.ts:2820-2847)。

**合并算法**(钩子顺序执行,首个 deny 短路):

```
verdict = None
for hook in 匹配钩子(配置顺序):
    if verdict == "deny": break                  # deny 是终局,后续钩子不再执行(记 skipped)
    result = await hook.run(input)               # 单钩子执行 + 审计(§8)
    if result.permissionDecision == "deny":
        verdict = "deny"; deny_reason = hook 名 + permissionDecisionReason
    elif verdict is None and result.permissionDecision == "allow":
        verdict = "allow"; allow_hook = hook; item.immune = result.immune(仅同结果 allow 时)
    if result.permissionDecision != "deny" and result.updatedInput is not None:
        item.input = result.updatedInput          # last-wins;deny 终局,deny 钩子的改写不生效(对齐 CC)
                                                  # 仅 allow/passthrough 钩子的 updatedInput 透传
```

| 合并结果 | 动作 |
|---|---|
| 任一 deny | 返回拒绝 ToolResult(`Permission denied by hook {name}: {reason}`,error_code=permission_blocked);updatedInput 不生效(deny 终局);审计 source=`hook:PreToolUse` |
| 无 deny 且任一 allow | **allow 短路**:`item.hook_allowed=True`,引擎决策链不运行;唯一例外 = 写保护地板(§5.3);审计 source=`hook:PreToolUse` |
| 无决策(全部 passthrough/无输出) | 引擎照常运行(loop.py:542 `_permission_check` 原样);钩子不产生权限审计事件 |

### 5.3 allow 短路与写保护地板

**allow 短路定义**:钩子 allow 后引擎决策链不再逐条求值(deny 规则、ask 规则、yolo 等全部跳过)。语义等价 CC 的 `{behavior:'allow'}`(toolHooks.ts:520-528)。

**唯一例外——写保护地板**:CLAUDE.md 不变量「写保护路径优先于 allow」无例外条款(engine.py:113-119 是硬地板,显式 allow 规则也不能写保护路径)。钩子 allow 不得突破它。实现:`PermissionEngine.floor_check(...)`(**engine.py 新增方法**,仅含第 4 步逻辑:FILE_TOOLS + 目标路径 + is_write_protected)→ 命中则返回 `PermissionDecision(False, "ask", "write-protection", ..., requires_explicit_approval=True)`,由 loop 侧按既有 ask 流程处理(request_permission 人工确认);未命中返回 None。

- floor_check 命中时审计:emit ToolAuditEvent(engine.py:186-195 既有 `_decide` 路径,source=`write-protection`)——地板命中时钩子 allow 被降级为人工确认,**审计如实记录**。
- **Bash 地板(实现已扩展)**:Bash 与 FILE_TOOLS 同等地板——floor_check 对 Bash 复用 `analyze_bash_command` 的 deny 判定(`rm -rf ~` 类 rm/rmdir 保护路径,引擎必拒的命令不得被钩子 allow 绕过),并以 `rm_protected_targets`(bash_rules.py 新增)补查 rm/rmdir 目标是否命中写保护组件(`rm -rf .git` 类,paths.py 同款判定);任一命中 → 与文件工具同款降级 ask(requires_explicit_approval + request_permission)。非保护命令(如 `git status`)不受影响。
- 敏感路径(engine.py:136-145)/工作目录约束(:129-133)/显式批准清单(:154-158)不做例外:它们是「默认 ask」类策略,钩子 allow 是配置作者对本次工具使用的显式授权,授权方升级后不再询问(与 CC 一致)。**攻击面提示**(§11 风险):project 层 settings 可入库,恶意仓库可带 allow 钩子——缓解:快照语义 + 写保护地板 + 审计可追溯;local 层覆盖 project 层(settings.py:19)。

### 5.4 updatedInput 透传

- 来源:任意 PreToolUse 钩子的 `updatedInput`(无决策的 passthrough 钩子也可改写,对齐 CC toolHooks.ts:556-563 的 hookUpdatedInput 独立 yield)。
- **应用时机:pre_hook 内、引擎求值之前**——`item.input` 被改写后,tool_queue.py:163 的 `_permission_check` 与 :175 的 `tool.call` 读到的都是改写后输入(「改写后的 input 传给权限引擎和工具执行」,todo.md:98)。
- 多钩子改写:**last-wins**(顺序执行天然确定)。
- 改写不落会话:会话只记模型原始 tool_use 与工具结果(append-only 不变量);hook 改写属执行层派生,重放时钩子重跑自然重现——与 08 的 reminder 不持久化同理(loop.py:384-388)。

### 5.5 safetyCheck bypass-免疫位

**语义**:免疫位标记「本次工具调用已被信任层担保」,未来自动化安全检查(safetyCheck = 阶段 16 的 LLM 意图闸门/破坏性守卫等 fail-closed 检查)对免疫项**跳过**。CC 的 immune-bit/trusted-tool 概念在参考产物中无对应源码段落(产物 §9 仅提信任检查),故语义由本规格定义,阶段 16 规格据此消费。

**设置**:仅一条路径——PreToolUse 钩子输出 `permissionDecision=allow` **且** `immune: true`(同一条钩子结果)。落在 `ScheduledTool.immune`(tool_queue.py **新增字段**)。

**防滥用约束**:
1. `immune` 无 `allow` 同结果 → 免疫位忽略 + validation 警告(不能「只豁免检查、不负责授权」);
2. 免疫位**不豁免任何权限层**:写保护地板、引擎 deny、钩子 deny 均不受影响(deny 绝对优先);
3. 免疫位**不豁免其他钩子**:后续钩子照常执行(deny 短路规则不变);
4. 每次设置免疫位 → 审计事件记录 `immune=true`(§8),豁免可追溯。

**v1 消费面**:v1 无 safetyCheck 层,免疫位只做「设置 → 携带(ScheduledTool)→ 审计」;阶段 16 的 bash-safety 规格将定义「safetyCheck 跳过免疫项」的消费规则。**v1 不新增任何绕过效果**——这是契约预留 + 审计占位,不是旁路。

## 6. 主循环接线

### 6.1 执行顺序(单工具全链路)

```
tool_queue.py:158-159 abort 检查
  → :161-162 pre_hook        (PreToolUse 钩子:执行 + 决策合并 + updatedInput/immune 落位)
  → :163 引擎(或 allow 短路跳过)
  → :175 tool.call(改写后 input)
  → :200-201 finalize(PI-02,既有)
  → :202-203 post_hook       (PostToolUse 钩子)
```

即 pre_hook → permission → execute → finalize → post_hook。denied 分支走 :167-168(拒绝结果也触发 PostToolUse)。

### 6.2 逐事件接线表

| 事件 | 接线 | 说明 |
|---|---|---|
| SessionStart | run() 入口(loop.py:154-160 之间,**新增**调用);门闩 `self._session_started`(loop.py **新增字段**,首个 run() 置位后不再触发) | build_loop(assemble.py:28-67)是同步函数,不能 await;钩子执行需事件循环,故挂 run() 首部而非装配点。`source`:history 非空(loop.py:166)→ `"resume"` |
| UserPromptSubmit | loop.py:160(首条)与 loop.py:224(steer),均在 `user_message()` 之前(**新增**调用) | blocked(exit 2)→ 首条:yield `assistant_message(stderr, is_meta=True)` + return,`last_stop_reason="hook_blocked"`;steer:静默丢弃 + 日志。`updatedPrompt` 替换文本后再建消息 |
| PreToolUse | loop.py:534-539 传 `pre_hook=self._pre_tool_use_hook`(**新增**方法)→ tool_queue.py:161-162 | 见 §5 |
| PostToolUse | loop.py:534-539 传 `post_hook=self._post_tool_use_hook`(**新增**方法)→ tool_queue.py:167-168/202-203 | 成功与拒绝都触发;finalize 之后(顺序固定 :200-203) |
| Stop | completed 分支(loop.py:262-278 的 return 前)与 tool_terminated 分支(:304-308 的 `_stop` 前)(**新增**调用) | 见 §6.4 门控 |
| PreCompact | **封装进 `_compact` 内部**(compaction.py:289 `generate_summary` 调用前;loop.py:205-207 auto 主路径与 :246 PTL 路径**一处覆盖,零遗漏**) | `_compact` 加 `trigger: str = "auto"` 参数(阶段 10 manual 预留)。exit 2 → 阻止压缩:本轮不压缩、无指令(防抖 loop.py:206 已占位,下轮 turn+1 正常恢复,与 CC「阻止本次压缩」语义一致,无需改防抖逻辑);exit 0 + stdout → 多钩子输出 join('\n\n') 作为 custom instructions 注入摘要 prompt(§7.4);exit 1/超时/无输出 → 压缩照常、无指令(**fail-open**)。**与既有机制零冲突**:熔断 `_compact_failures` 只由 generate_summary 的 LLMError 驱动(loop.py:344-347),PreCompact 钩子失败/exit 2 不计入(不误伤熔断);压缩检查点的请求视图投影(loop.py:199-204)独立于钩子;abort 置位时跳过钩子、压缩照常(§6.3);压缩不计 turn 的不变量不变 |
| PostCompact | **封装进 `_compact` 内部**(成功返回 summary 前;loop.py:208-212/:247-252 双路径自动覆盖) | 纯观察型:exit 2 与非阻塞等同(§4.3 PostToolUse 同款);`compact_summary`(summary_msg.content)、`cut_index`(cut.index)进 HookInput |
| Notification | 四个 emit 位各 1 行 `notify(...)`(**新增**调用):loop.py:555-557(permission_request,request_permission 之前)/loop.py:558(permission_denied)/tool_queue.py:183-184(tool_error)/loop.py:309-317(llm_error) | 见 §2.5;fail-open + 默认超时 10s;通知 hook 失败不影响权限询问/错误路径 |

### 6.3 执行模型:顺序 + abort 感知

- **顺序执行**(与 CC 并行、结果聚合相反):确定性审计、fail-closed 推理简单;子进程粒度下 v1 钩子数量少,性能可忽略。文档化该分歧。
- **AbortSignal 感知**(保留清单 #4,codesage.md:35):每个事件入口检查 `self.abort.is_set()`(与三检查点 loop.py:180/281/424 对齐);钩子批次执行中 abort 置位 → 跳过剩余钩子,**不产生决策**(引擎照常/中断语义不变)。运行中钩子子进程无法强制终止(v1 边界,Windows kill 复杂度),受 timeout 约束自然结束。
- 钩子自身异常(非 HookError 的 bug)→ 捕获转非阻塞错误 + 日志,不拖垮主循环(对齐 tool_queue._emit_tool_event 的 best-effort 规则 :89-101)。

### 6.4 Stop 门控与交互

| stop reason | 触发 Stop 钩子? |
|---|---|
| completed / tool_terminated | ✅ |
| max_turns / max_budget / interrupted / error / thinking_only_exhausted / hook_blocked | ❌(控制流终止:用户要退出时钩子不得复活循环) |

Stop 钩子结果:
- exit 2 → 注入 `user_message(f"Stop hook feedback:\n{stderr}")`,**继续循环**(模型看到反馈再决策);注:CC 的 meta 语义(stopHooks.ts:257-267,「UI 隐藏但模型可见」)在 CodeSage 无对应消息类型——本地 `is_meta` 会被 normalize_for_api 过滤(normalize.py:69),模型不可见,故落地为**普通 user_message 普通历史**,模型可见、进会话日志;注入后下一轮计为一次 turn;
- **注入上限(M1,09 实现补)**:`MAX_STOP_HOOK_ATTEMPTS = 5`(对齐 CC 同名单),loop 层按 **run() 生命周期**计数(loop.py 入口重置,与 turn 同生命周期)——每次真正注入时递增,达限后不再注入 feedback、按普通 completed/tool_terminated 停止,**不报错**;防「永远 exit 2」的钩子拖出无限循环;
- `continue:false` + `stopReason` → `_stop("hook", stopReason)`(stopReasonOverride 即此位);
- 钩子层自身异常 → 只警告 + 日志,不影响停止(CC fail-open,stopHooks.ts:456-472)。

**与 auto-compact / steer 的交互**:
- UserPromptSubmit 钩子在 compaction 检查点(loop.py:193-212)之前运行;钩子注入的 additionalContext/updatedSystemReminder 是下一次请求的一次性 prefix(§7),compaction 不感知(请求视图)。
- Stop 钩子注入的 feedback 消息进入消息流,下一轮 compaction 视其为普通历史。
- steer 输入被钩子 blocked 只影响该条输入,不中断运行。

## 7. 消息改写语义

### 7.1 改写通道(全部请求视图,不落会话)

| 通道 | 事件 | 实现 | 落会话? |
|---|---|---|---|
| updatedInput | PreToolUse | 改写 `item.input`(§5.4);引擎与执行读同一字段 | 否(执行层派生,重放时钩子重跑) |
| updatedPrompt | UserPromptSubmit | loop.py:160/224 处替换 user_input 后 `user_message()` | 会话记改写后文本(它就是这次对话的真实输入) |
| updatedSystemReminder | UserPromptSubmit | 存一次性字段 `self._hook_reminder`(loop.py **新增**),下次 `_ask_model` 经 prefix 机制注入(§7.2) | 否(prefix 只进请求,loop.py:384-388) |
| additionalContext | SessionStart / UserPromptSubmit | 同 updatedSystemReminder,渲染为 reminder 段 | 否 |
| compactInstructions | PreCompact | 多钩子 stdout join('\n\n') 后追加为 `_summary_prompt` 的 `# Custom Instructions` 段(§7.4) | 否(请求视图内构造,不进会话) |

### 7.2 updatedSystemReminder 的 byte-stable 约束

注入位 = loop.py:382-400 的 prefix 组装(现 context bundle reminder + recovery reminder 两位)。`_hook_reminder` 作为**第三位 prefix 消息**,插在 context bundle reminder 之后、历史消息之前——**位置固定**,仅内容随钩子触发变化。约束:

- 渲染复用 REMINDER_HEADER/FOOTER(loop.py:56-60);不得超过 MAX_REMINDER_SECTIONS 预算的语义冲突由钩子作者负责(它是一条独立消息,不算 section);
- **一次性消费**:注入后清除(loop.py:389-398 的 `_recovery_reminder` 同款模式);
- 内容变化会命中 §3.9 缓存断裂检测(loop.py:437-444)——检测只记日志不动作,钩子作者须知:高频 updatedSystemReminder = 主动打破前缀缓存(与 CC 语义一致:该字段是罕见显式操作);
- **不得**改写 `_render_reminder`(loop.py:585-609)的 section 逻辑——那会污染 08 的 10 段预算与固定前缀。

### 7.3 与 ToolResult.new_messages 的关系(顺带处置)

`ToolResult.new_messages`(tools/base.py:68,注释「injected into the conversation (phase 06)」)从未被主循环消费,也无任何工具设置它(全仓仅 tool_queue.py:51/195 透传保留)。**处置:明确弃用**——理由:钩子系统的 updatedInput/additionalContext 已是受审计、经 normalize 语义的合法注入通道;new_messages 绕过 normalize 与审计,是未定形接口。做法:tools/base.py:68 注释改为 `deprecated (phase 09): use hooks channels; retained for compatibility, remove in a later phase`;**不删除字段**(删除会破坏 ToolResult 构造契约与透传)。阶段 12+ 清理。

### 7.4 PreCompact custom instructions(压缩指令注入)

PreCompact 钩子 exit 0 + stdout → 多钩子输出 `join('\n\n')` 作为 custom compact instructions(对齐 CC executePreCompactHooks,hooks.ts:3961-4025)。**注入点** = `_summary_prompt`(compaction.py:221-227)——SUMMARIZATION_PROMPT 与 UPDATE_SUMMARIZATION_PROMPT 都经它 format:prompt 尾部追加 `# Custom Instructions` 段。改动:compaction.py 的 `_summary_prompt`/`generate_summary`(:289)加 `extra_instructions: str | None = None` 参数,`_compact` 传钩子输出;PTL 截断重试的 `_summary_prompt` 调用(:273)同样拼接(extra_instructions 一路透传)。

- 指令**不落会话**(请求视图内一次性构造,与 §7.1 同哲学);
- 无输出(exit 1/超时/空 stdout)→ 无指令,压缩照常(fail-open,§6.2 PreCompact 行);
- exit 2 → 本轮不压缩,指令无从谈起;
- **无 JSON 解析**(指令输出是纯文本 join)——hookSpecificOutput 校验对 PreCompact 无安全意义(压缩不是安全门,§1.2 裁剪表)。

## 8. 审计与调试

### 8.1 双流审计(与「每决策恰好一条审计事件」不变量兼容)

1. **权限流(audit.jsonl,既有 sink)**:钩子产生决策时,由钩子层 emit `ToolAuditEvent`(audit.py:19-30 同款结构)——allow 时 source=`hook:PreToolUse`,deny 时同;reason 含钩子名。引擎运行时事件照旧。**每个工具恰好一条权限事件**(钩子决策时引擎不跑,引擎跑时钩子无决策——两条路径互斥)。floor_check 命中时事件 source=`write-protection`(引擎 _decide 路径)。**floor 降级是第二次决策,再记一条审计事件**——hook allow(第 1 条,source=`hook:PreToolUse`)+ 地板降级 ask(第 2 条,source=`write-protection`);「每决策恰好一条」指每条决策事件自身,不变量合规。
2. **执行流(hooks.jsonl,新增)**:每次钩子调用恰好一条 `HookAuditEvent`(字段见下),追加写 + fsync(复用 JsonlAuditSink 的写入模式,audit.py:43-50)。实现:一行子类 `class HookJsonlSink(JsonlAuditSink): pass`(hooks/_common.py **新增**)——emit 的 `asdict(event)` 是泛型的,audit.py 零改动。

```python
# hooks/types.py 新增
@dataclass(slots=True)
class HookAuditEvent:
    event: str            # SessionStart | UserPromptSubmit | PreToolUse | PostToolUse | Stop | PreCompact | PostCompact | notification_type 值(permission_request 等)
    hook_type: str        # command | prompt
    command: str | None   # 命令/prompt 摘要(截断 200 字符)
    matched: bool
    outcome: str          # success | blocked(exit2) | non_blocking_error | timeout | validation_error | cancelled(abort跳过) | skipped(deny短路)
    exit_code: int | None
    duration_ms: int
    stderr_summary: str | None   # 前 200 字符
    timestamp: str
```

- 权限决策审计不落 stdout/stdin 内容(input_summary 只留路径类字段,对齐 engine.py:245-251 的 `_summarize` 哲学——**钩子输入输出内容不落审计**,防密钥/敏感数据)。
- 装配:assemble.py:45-46 处 JsonlAuditSink 已建,引用传入 `load_hook_manager`;hooks.jsonl 路径 = `paths.config_dir() / "hooks.jsonl"`(assemble.py **新增一行**)。

### 8.2 调试日志

- 每次钩子执行:logger 记录事件/钩子名/exit_code/duration_ms/outcome(debug 级;失败与超时 warning 级)。
- 配置解析失败条目、matcher 非法正则、事件不匹配字段:启动时 warning。
- 所有日志带 `codesage.hooks` logger。

## 9. 测试计划

### 9.1 镜像清单(`tests/hooks/test_<file>.py`,镜像 `codesage/hooks/`)

| 测试文件 | 覆盖 |
|---|---|
| test_types.py | HookJSONOutput 校验矩阵:合法全字段 / 未知字段拒绝 / 事件不匹配字段拒绝 / immune 无 allow 忽略 / permissionDecision 枚举;HookAuditEvent 序列化 |
| test_registry.py | settings.hooks 解析(事件→组→钩子);matcher 三级(管道/正则/空/非法正则);三层合并(跨层拼接 + 身份去重,settings.py:46-56);非法条目丢弃 + 日志;快照语义(解析后改配置不生效) |
| test_command.py | 子进程执行(stdin JSON / env / cwd);stdout JSON 解析与 plainText 分支;退出码 0/1/2/其他 全表;超时 → fail-closed;spawn 失败;stderr 捕获与截断;Windows Git Bash 选择(_shell_argv 模式) |
| test_prompt.py | prompt 模板 `$ARGUMENTS` 替换;`{ok,reason}` 契约(系统提示强制 JSON + 客户端严格校验,落地差异见 §4.8;含字段缺失/非法输出);ok:false 按事件区分(PreToolUse → deny 且 reason 进审计与拒绝文案 / Stop → 阻止停止 / UserPromptSubmit → 阻止提交);JSON 解析失败/超时 → fail-closed(PreToolUse deny,Stop 放行+警告);ok:true → 无决策;模型指针 quick + 失败回退 main;mock client 调用形状 |
| test_manager.py | **决策合并矩阵**(§5.2):deny 赢 allow / 多 deny / allow 短路(引擎不跑,断言 `item.hook_allowed`)/ 无决策 → 引擎照常 / updatedInput last-wins 且先于引擎 / **deny 钩子的 updatedInput 不生效**(§5.2 守卫)/ deny 后后续钩子 skipped / immune 仅 allow 生效;**写保护地板**:allow + 保护路径 → 降级 ask(requires_explicit_approval=True);**地板+immune 组合**:allow+immune 命中写保护 → 仍降级 ask(免疫位不豁免权限层,§5.5 约束 2);**审计**:每工具恰好一条权限事件(钩子决策 / 引擎决策两路径)、floor 降级第二条事件(§8.1)、HookAuditEvent 每条一次;**执行流水线**(§4.10):无配置事件短路不 spawn(索引空 → 零路径断言)、同批重复钩子只执行一次且只审计一次(§4.10.3)、stdout 超限截断(>256KB,截断 JSON → fail-closed,§4.10.5)、聚合传递链(additionalContext 多钩子 join('\n\n')、updatedInput last-wins、exit 2 → blockingError、逐事件消费总表 §4.10.6) |
| test_loop.py(engine,扩充) | pre_hook/post_hook 接线:denied 分支触发 post_hook;钩子 deny 不株连 sibling(permission_blocked 豁免,tool_queue.py:127-135);UserPromptSubmit:blocked(首条 yield+return / steer 静默)、updatedPrompt、updatedSystemReminder 一次性注入(第二次请求无残留);**updatedInput 改写不落会话**(§5.4:会话 JSONL 只记模型原始 tool_use 与工具结果,断言无改写痕迹);Stop:completed 触发 / max_turns 不触发 / exit 2 → 继续 / continue:false → `_stop("hook", reason)`;SessionStart:门闩一次、source 判定;abort 置位跳过钩子;finalize → post_hook 顺序(test_loop.py:477 既有 finalize 测试保持绿) |
| test_http.py(**新增**) | URL 白名单(空 `[]` 全禁 / `*` 通配 / 未命中 → 不执行 + warning);header 插值白名单($HOME 不可替换,未列入 → 空字符串);CRLF 消毒(插值后剥 \r\n\x00);SSRF 矩阵(内网/云元数据/链路本地拒绝、127.0.0.1 放行);非 2xx → fail-closed(PreToolUse deny);非 JSON body → fail-closed;空 body → `{}` 成功;超时 → fail-closed;max_redirects=0(httpx.MockTransport 注入,不真发请求) |
| test_if_rules.py(**新增**) | 语法解析(`"Bash(git *)"`/`"Read(/src/**)"`/裸工具名/括号不闭合 → 恒 false);Bash 匹配(精确/`*` 前缀通配/空格归一化/不匹配);文件工具(file_path 精确/`/**`/`/*`;LS/Glob 的 path 字段;路径缺失 → false);工具不存在/工具名不匹配 → false;其他工具(WebFetch)内容规则 → 工具级匹配;非 PreToolUse/PostToolUse 事件带 if → warning + 永不执行;执行器集成:if 不匹配 → command 钩子进程计数 0(不 spawn)、matcher 命中但 if 未命中 → 不执行;validate_input 抛错 → false |
| test_compact_events.py(**新增**) | auto 主路径触发(loop.py:205-207);PTL 反应式路径触发(:246);PreCompact exit 0 + stdout → 摘要请求 prompt 含 `# Custom Instructions`(mock client 断言 `_request_summary` 收到的 prompt);exit 2 → `generate_summary` 不被调用、防抖已占位下轮可再触发;exit 1/超时/无输出 → 压缩照常无指令(fail-open);PostCompact:压缩成功后触发、`compact_summary` 含摘要文本、`cut_index` 正确、观察型(exit 2 无效果);钩子失败不增 `_compact_failures`(熔断不误触) |
| test_notification.py(**新增**) | 四源 emit(permission_request/permission_denied、tool_error、llm_error);notify 分发(matcher 取 notification_type);超时 fail-open(10s,不拖慢权限询问/错误路径);exit 2 非阻塞;**通知不产生权限审计事件**(audit.jsonl 无新增,§9.2 红线);statusbar 消费冒烟 |

### 9.2 不能破坏的既有契约(09 改动红线)

| 契约 | 测试 | 保护 |
|---|---|---|
| settings.hooks 字段透传 | test_settings.py:92-97 | 09 只消费,不改字段定义 |
| settings 任意 dict 透传(extra=allow) | test_permissions/test_store.py:36-39 | 同上 |
| finalize 结果改写先例 | test_loop.py:477-491 | 顺序固定 finalize → post_hook(tool_queue.py:200-203) |
| 权限决策审计恰好一条 | engine 既有审计断言 | §8.1 双流互斥 |
| 队列并发屏障/错误结果/sibling 政策 | test_tool_queue.py 既有 | :161-169 改造只动 pre_hook 与 `not item.hook_allowed` 守卫,不动 barrier 与 sibling 逻辑 |
| abort 检查点(180/281/424) | test_loop.py 既有 | 钩子执行不移动检查点,仅在其间插入 abort 感知 |
| 压缩防抖/熔断语义(**新增**) | test_compact_events.py | `_last_compact_turn` 防抖(loop.py:113/206)与 `_compact_failures` 熔断(loop.py:114/344-347)语义不变:PreCompact 钩子失败/exit 2 不计入熔断,exit 2 后防抖仍占位 |
| 通知不产生权限审计事件(**新增**) | test_notification.py | §2.5:通知是状态行,非决策;「每决策恰好一条」不变量不受影响 |
| HTTP 白名单默认全禁(**新增**) | test_http.py | settings 顶层 `http_hook_urls` 默认 `[]`;未命中白名单的 URL 永不执行 |

## 10. 实施步骤

| # | 步骤          | 内容 | 依赖 | 验收(测试) |
|---|-------------|---|---|---|
| S1 | 契约层         | `hooks/types.py`:HookInput/HookJSONOutput/HookSpec(含 `if?` 可选字段、http 类型 url/headers/allowedEnvVars、notification_type 枚举校验)/HookAuditEvent + 校验器;`hooks/base.py`:HookExecutor 协议与 HookResult | — | test_types.py:校验矩阵全绿 |
| S2 | 匹配与解析 + if 过滤 | `hooks/_common.py`:matcher(三级)+ `if_rule_matches`(§2.4,~40 行,复用 rules.py)+ settings.hooks 解析(含 http 类型、`http_hook_urls` 白名单、非可求值事件带 if → warning)+ HookJsonlSink 一行子类 | S1 | test_registry.py:matcher/合并/丢弃/快照;test_if_rules.py:语法/Bash/文件工具/恒 false/事件剔除 |
| S3 | 命令执行体       | `hooks/command.py`:子进程(复用 _shell_argv 模式)、stdin JSON、超时、退出码、stdout 解析、fail-closed | S1 | test_command.py:全表 |
| S4 | HTTP 执行体    | `hooks/http.py` **新增**:POST + max_redirects=0 + 必须 JSON 契约 + 60s 超时 + URL 白名单(默认 `[]` 全禁)+ header 白名单插值 + CRLF 消毒 + SSRF 矩阵 + 仅 SessionStart 禁用 | S1 | test_http.py:全矩阵(httpx.MockTransport) |
| S5 | HookManager | `hooks/registry.py`:事件分发(事件→钩子数索引,§4.10.1 零配置短路)、顺序执行、决策合并(§5.2)、updatedInput/immune 落位、if 过滤(spawn 前,hook 级)、执行层去重(§4.10.3)、结果聚合(§4.10.6)、`notify()` 方法(§2.5)、abort 感知、双流审计 | S1, S2 | test_manager.py:合并矩阵 + 执行流水线(短路/去重/限额/聚合传递)+ 审计 |
| S6 | 引擎接线        | loop.py:534-539 传 pre/post_hook;tool_queue.py:156-169 流程改 + ScheduledTool 新字段;`PermissionEngine.floor_check`(engine.py 新增) | S5 | test_loop.py:接线/豁免/地板 |
| S7 | 事件接线        | SessionStart 门闩 + UserPromptSubmit(loop.py:160/224)+ Stop 门控 + prefix 注入(`_hook_reminder`,loop.py:382-400 扩展) | S5 | test_loop.py:各事件 + 一次性语义 |
| S8 | compact 事件接线 | `_compact`(loop.py)加 `trigger: str = "auto"` 参数 + 钩子两点(PreCompact:generate_summary 前;PostCompact:成功返回前,loop.py:208-212/:247-252 双路径一处覆盖);compaction.py `_summary_prompt`/`generate_summary` 加 `extra_instructions`(:221-227/:273/:289 透传);事件注册 + matcher(trigger) | S5 | test_compact_events.py:auto 主路径/PTL 路径/exit 2 阻止/注入断言/fail-open/熔断不误触 |
| S9 | 通知 emit     | 四 emit 位各 1 行 `notify(...)`(loop.py:555-557/558/309-317、tool_queue.py:183-184)+ statusbar 消费(repl.py:165-176 处) | S5 | test_notification.py:四源/超时 fail-open/审计红线 |
| S10 | 提示执行体 + 装配  | `hooks/prompt.py`(LLMClient + quick 指针 + `$ARGUMENTS` + {ok,reason});assemble.py 装配(load_hook_manager + hooks.jsonl) | S6 | test_prompt.py;assemble 冒烟 |
| S11 | 收尾          | new_messages 弃用标注(tools/base.py:68);docs/modules/09-hooks.md;todo 勾选;全量回归(515 + 新增) | S8, S9, S10 | 全量 pytest -q 全绿 |

**依赖图**:S1 → S2 → S5;S1 → S3 → S5;S1 → S4 → S5;S5 → S6 → S7;S5 → S8;S5 → S9;S6 → S10。(S3/S4 可并行;S7/S8/S9/S10 可并行)

## 11. 风险与边界

- **project 层 hooks 是信任注入点**:project settings 可入库,恶意仓库可带 allow/immune 钩子或 exit 2 钩子干扰。缓解:快照语义(会话中不热载)、写保护地板不可突破、双流审计可追溯、local 层覆盖 project 层;09 规格在 docs/modules 明示「hooks 配置 = 可执行代码声明」。**信任决策见 §4.10.7**:无信任门(无 CC 式信任对话框/trustLevel),「集中式检查位」由配置解析期一次性校验 + 快照冻结承担;边界:首次 clone 恶意仓库 project 钩子会执行(与 CC 拒绝信任后跳过相反),v1 接受,代价由快照 + 地板 + 审计承担。
- **执行层去重缺失的后果**(新增):§4.10.3 的 `(type, command|prompt|url, if)` 同批去重若不做,同一钩子被多个 matcher 组命中或 user/project 同内容不同实例 → 重复执行(副作用翻倍)+ 两条审计事件(违反 §8.1「每次钩子调用恰好一条」不变量)+ §5.2 合并中 allow 被重复计数。缓解:去重是管线内建步骤(§4.10 ③,last-wins 保留配置序靠后者),test_manager 有「只执行一次只审计一次」断言。
- **fail-closed 的可用性代价**:PreToolUse 超时即 deny——慢钩子会系统性阻塞工具执行。缓解:默认 60s 足够宽裕、逐钩子可配、hooks.jsonl 暴露 duration 便于诊断。
- **updatedSystemReminder 与缓存**:内容变化主动打破前缀缓存(§3.9 检测器会记日志)。预期行为,文档化。
- **Windows 子进程终止**:钩子超时杀进程用 taskkill 树杀(asyncio.create_subprocess + 进程组),v1 不做 abort 主动杀(仅超时杀);文档化。
- **prompt 钩子成本**:每次触发一次 LLM 调用(quick 指针),走既有重试/回退;VCR 可录制集成测试(CODESAGE_VCR,ai/vcr.py)。
- **钩子输出不被信任**:输出经严格校验(字段/类型/事件名),恶意 JSON 无法注入行为;JSON 以 `{` 开头才解析,plainText 永远只是文本。
- **HTTP 钩子的 SSRF/数据外泄面**(新增):四层防护——URL 白名单默认全禁、header 白名单插值、CRLF 消毒、SSRF 矩阵(§4.9);配置作者仍可能显式放行内网 URL,快照语义 + 双流审计可追溯(project 层配置入库风险同首条)。仅 SessionStart 禁用将关键路径与外部网络隔离。
- **通知源分散易漏**(新增):4 个 emit 位分布在 loop.py 与 tool_queue.py 两文件,后续新增错误/状态路径时可能漏接。缓解:`notify` 是唯一入口、emit 位集中在两文件、test_notification.py 覆盖四源;新增源时在 §2.5 表中登记。
- **PreCompact 指令的注入面**(新增):钩子 stdout 文本直接进摘要请求 prompt,是提示注入面(钩子作者可控,信任面同 settings 配置)。缓解:注入段以 `# Custom Instructions` 分隔、fail-open(钩子失败无指令)、配置快照语义;摘要输出仍经既有校验。

## 12. 与路线图的关系

- 落地 codesage.md:41 保留清单 #10(权限决策改写 + updatedInput)、codesage.md:148 阶段 09 全项。
- 阶段 16(bash-safety)消费免疫位与 fail-closed 先例;阶段 10(compact)消费 PreCompact 的 manual trigger(trigger 字段与 `_compact` 接线已预留,届时补 repl.py:369-378 find_command 注册 + loop 公开 compact_now() 方法);阶段 13 提供 agent 钩子基建;阶段 19(plugins)可用同一配置解析扩展插件钩子(CC 的 pluginRoot 概念)。
- 本阶段的 `HookAuditEvent` 流即审计扩展的先例:阶段 12(会话生命周期)与阶段 16(安全)可直接消费 hooks.jsonl。
