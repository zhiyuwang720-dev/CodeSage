# 阶段 07 — CLI REPL 与 V1 闭环理解文档

> 分支 `feat/07-cli`,规格见 `docs/specs/07-cli.md`。**V1 主线完成站。**

## 模块职责

终端前端:把 01–06 装配成可用产品。两种模式:
- **交互 REPL**(`codesage` 或 `python -m codesage.cli`)—— 逐轮对话
- **单次模式**(`codesage "任务"`)—— 非交互,ask 决策拒绝(安全默认),验收自动化走这条路

## 关键设计决策

### 1. 装配根(composition root)

`assemble.build_loop()` 是唯一装配点:settings → LLMClient → 工具注册表 → 权限引擎(审计 sink)→ Session → AgentLoop。阶段 06 的 AgentLoop 完全依赖注入,CLI 只是把线接好 —— 阶段 19(热插拔)改造的正是这一处。

### 2. 单次模式的权限语义:ask → 拒绝

单次模式没有 UI,`request_permission=None` —— ask 决策直接变拒绝(引擎默认)。**这产生了真实有趣的 agent 行为**:模型收到 "Permission denied: Bash requires explicit approval" 后,自动调整策略 —— 冒烟实测中它放弃执行,改为给用户输出命令建议。这是「错误自愈」设计在真实世界的完整演示。

### 3. 权限询问(y/n/remember)

交互模式:`request_permission` 接终端 —— `(y)es / (n)o / (r)emember`。**remember 写入 settings.local.json 的 permissions.allow**(阶段 05 的 store),下次同工具直接放行。这是权限系统的人机闭环:ask → 批准 → 记住 → 规则沉淀。

### 4. env 配置(CODESAGE_MODEL / CODESAGE_BASE_URL / CODESAGE_API_KEY_ENV)

CLI 的默认(main)profile 可用环境变量指向任意 OpenAI 兼容端点 —— 零配置文件即可用:
```bash
CODESAGE_MODEL=deepseek-v4-flash CODESAGE_BASE_URL=https://api.deepseek.com/v1 \
CODESAGE_API_KEY_ENV=DEEPSEEK_API_KEY codesage "任务"
```
全局配置(GlobalConfig.model_profiles)是进阶路径,env 是 V1 的快捷路径。

### 5. 打包:console script + 包内 __main__

修了一个真实工程坑:`codesage/cli/__main__.py`(顶层入口)与包内 `codesage/codesage/cli` 同名冲突 → `python -m codesage.cli` 报 "is a package"。正解:入口放包内 + `pip install -e .` + pyproject `[project.scripts] codesage = "codesage.cli:main"`。标准打包路径,CLI 以后就是可安装产品。

## 冒烟实测(真实 API)

```
You: 回复 V1 冒烟测试通过即可
◈ LS path=...
◈ Glob pattern=**/*test*
  (thinking: 382 chars)
  ✓ tool[Ia3689] .pytest_cache/ codesage/ ...
  ✓ tool[qP6363] tests tests/conftest.py ...
◈ Bash command=cd ... && python -m pytest ...
  ✗ Permission denied ...
```

模型**自主执行**了 LS → Glob → Bash 的探索链,并在权限拒绝后优雅调整 —— 一个真实的 agent 循环在 0.01 版本上跑起来了。

## 完成标准(对照规格,全部实测)

- [x] 单次模式:真实 API 创建文件任务完成(`test_v1_acceptance_create_file` 通过)
- [x] deny 规则:任务不执行,模型收到拒绝(`test_v1_acceptance_deny_blocks` 通过)
- [x] 审计记录存在(Write allow + Write deny 事件断言)
- [x] 交互 REPL(手动冒烟:`--mode`/`--show-thinking`/斜杠命令可用)
- [x] 170 项全量单测绿

## 已知简化(ponytail)

- thinking 默认摘要显示(`--show-thinking` 全显)—— 阶段 08 上下文感知后优化
- 渲染纯文本 + 基础 ANSI,无 rich —— 富 TUI 明确 out-of-scope
- Windows 上 SIGINT 在 input() 期间被吞 —— 轮间 abort 检查兜底
- `/show-thinking` 需重启生效(仅演示 toggle,无状态持久化)

## 阶段衔接

- 阶段 08(context):base_prompt 换完整分层组装 + AGENTS.md
- 阶段 09(hooks):权限询问可被 PreToolUse 钩子改写
- 阶段 12(session):`--resume` 在 session 存储上叠加
- 阶段 15(mcp):装配根接入 MCP 工具注册
- 阶段 19(plugins):装配根 = 热插拔改造点

## 生产级强化(2026-08-05)

三轮修复(对照 Kode 审查,测试 170 → 337):

**修复内容**(批次 2 cli + 批次 3 C1-C7):
- [高] 非 TTY stdin → 单轮执行:stdout 非 tty + 输入存在自动 headless(解锁 CI/脚本场景)
- [高] `--resume`/`--session-id`(会话列表 + most_recent)—— 会话系统从此有入口
- [高] `--safe`(锁定 default 模式,永不 yolo)+ root 防护 + `--allowedTools`/`--disallowedTools` + 单轮权限逃生口(yolo + 精确 allowlist)
- [中] 优雅退出(SIGTERM/SIGBREAK + 退出码语义)+ `/show-thinking` 真开关(不重启即生效)
- [P0] `-p/--print` + headless 自动判定(C1);`--max-budget-usd` CLI 暴露(C2)
- [P1] `--output-format json`(CI 解析,C3);`--debug/--verbose` 日志(C4);`--system-prompt/-file`(C5)
- [P2] flag 组合校验(互斥组,C6)+ 非交互权限语义文档化(C7)

**文件级判定**:
- A 类(已实现):C1-C7 七项全落地(文件级 A 类最集中的模块)
- B 类(映射阶段 X):上下文分层(08)、hooks(09)、会话生命周期(12)、MCP(15)、热插拔(19)
- C 类(理由):Ink UI 全家(~300 文件)、statusline/notificationCenter —— 富 TUI 明确 out-of-scope

**现状**:及格(偏弱) → 良好。CI/脚本场景从 0 分到全解锁,会话有 resume 入口,权限语义在 headless 下可组合精确放行;唯一挂起项是真实 API 验收(待 DEEPSEEK_API_KEY)。

## 设计决策剖析

### 为什么这么设计

1. **装配根(composition root)单点接线** —— build_loop 是唯一把 settings → LLMClient → 工具注册表 → 权限引擎(带审计 sink)→ Session → AgentLoop 接在一起的地方,AgentLoop 完全依赖注入。动机:任何入口(REPL/单次/未来 API/子代理)走同一装配;阶段 19 热插拔改造的正是这一处。
2. **单次模式 ask → 拒绝** —— request_permission=None,ask 决策直接拒绝并回传模型。动机:无人在场的安全默认;模型收到 "Permission denied" 后自愈调整(冒烟实测:放弃执行,改输出命令建议)。逃生口显式组合:--mode yolo + --allowedTools 精确白名单。
3. **y/n/remember 人机闭环** —— remember 写 settings.local.json 的 permissions.allow,且是精确粒度:Bash(`<cmd>`) 前 80 字符 / Edit/Write 的父目录 `/**`。动机:批准沉淀为规则,下次免问但不会"放行任意命令"。
4. **headless 自动判定 + 退出码纪律** —— stdout 非 tty 且有输入 → 自动单轮;退出码 0/1/2 语义化区分。动机:解锁 CI/脚本场景(批次 2 前为 0 分),机器可判断成败而非解析文本。
5. **信号协作式中断** —— SIGINT/SIGTERM 第一次 set loop.abort(中断当前轮),第二次 exit 130;Windows 补 SIGBREAK。动机:与引擎的 abort 检查点配套,中断产物是 is_meta 消息,持久化不破坏。

### 设计原则

- **安全默认**:单次模式 ask 拒绝、--safe 锁 default 且 POSIX root 拒绝运行
- **装配单点**:全部依赖在 build_loop 接线,模块互不直接 new
- **机器可读优先**:--output-format json、RunSummary、退出码语义、stderr 分离
- **平台稳健**:ANSI/glyph 按编码降级、stdout 非 tty 零 ANSI、SIGBREAK 注册
- **最小 UI**:纯文本 + 基础 ANSI,富 TUI 明确 out-of-scope

### 优点

- 验收自动化可跑:V1 acceptance 测试直接调 run_single_turn(可注入 out)
- 精确工具面控制:apply_tool_filter 物理移除工具,模型只见存活 spec,越权调用显示 Unknown tool
- /show-thinking 实时 toggle,不重启生效
- 单次与交互共用同一 run_single_turn/AgentLoop,行为一致(仅渲染开关不同)
- env 配置零文件可用(CODESAGE_MODEL/BASE_URL/API_KEY_ENV),体验门槛低

### 为什么不选用别的技术方案

| 备选方案 | 为什么不选 |
|---|---|
| rich / Textual / Ink 富 TUI | 富 TUI 明确 out-of-scope(Kode Ink 全家 ~300 文件);纯文本 + ANSI 零依赖、Windows 10+ 原生支持、CI 管道安全 |
| prompt_toolkit / readline 交互 | asyncio.to_thread(input) 零依赖够用;行编辑/补全是锦上添花,引入即新增依赖(Boundaries: Ask first) |
| click/typer 等 CLI 框架 | 标准 argparse + 互斥组已覆盖(flag 组合校验 C6);参数面稳定,框架收益小 |
| 默认 yolo / 默认放行 | 无 UI 时 ask 拒绝是安全默认;放行必须显式(--mode yolo + --allowedTools),fail-closed |
| 把历史全量喂给模型做 resume | 涉及压缩/上下文策略,属阶段 12;V1 resume 只做会话发现与展示(降级语义) |

## 面试问题整理

### 技术点清单

装配根(composition root)/ 单次模式权限语义(ask→拒绝、yolo+allowedTools)/ 退出码与信号语义 / resume 与 session / remember 精确粒度 / 渲染与平台稳健(编码降级)

### 面试问题与答案

**Q: 什么是装配根?为什么 CLI 要单独一个 assemble.py?**
**A: build_loop 是唯一把 settings → LLMClient → 工具注册表 → 权限引擎(带 JsonlAuditSink)→ Session → AgentLoop 接在一起的地方。AgentLoop 完全依赖注入,CLI 只接线。好处:任何入口走同一装配;阶段 19 热插拔改造的就是这一处;测试可绕开装配直接构造 AgentLoop 或替换部件。**
**深度衍生: 装配里最容易被忽略的一步?** → **审计 sink 与 settings 透传:PermissionEngine 不带 sink 会静默用 NullAuditSink —— 不是崩溃,而是"安全副产品消失"(验收测试断言 Write allow/deny 事件存在)。build_loop 显式接线杜绝了这个失败模式。**
**广度衍生: 对比 Spring 式 DI 容器,手写装配根的优势?** → **无反射无魔法:普通函数调用,类型检查/跳转/断点全有效,依赖图读代码即得。CodeSage 依赖树固定且浅,手写是更可读的选择;热插拔(阶段 19)也只是扩展这个函数,不是换框架。**

**Q: 单次模式(无 UI)下 ask 决策会发生什么?模型怎么应对?**
**A: request_permission 传 None,引擎 ask 决策在 loop._permission_check 里直接变成 "Permission denied: <reason>" 的 error tool_result 回传模型。模型自愈 —— 冒烟实测中它放弃执行,改为输出命令建议。headless 精确放行组合:--mode yolo + --allowedTools(apply_tool_filter 从注册表物理移除未列工具,模型只见过滤后的 spec)。**
**深度衍生: 为什么 yolo + allowedTools 比"默认放行"安全?** → **默认放行是 fail-open,任何工具都过;yolo + allowedTools 是"仅放行显式枚举,其余仍走决策链"的 fail-closed。--safe 还把 mode 锁 default、POSIX root 拒绝运行,自动化场景多一层保险。**
**广度衍生: CI 场景的权限注入通用模式是什么?** → **"无人在场 = 拒绝 + 可枚举白名单逃生口":git hooks 的特权操作需显式 allow,CI 用作用域受限的 token 而非全局授信。CodeSage 的 headless 语义是同一原则:自动化安全取决于显式授权面,不是环境可信度。**

**Q: 退出码语义是什么?SIGINT 怎么处理?**
**A: 0 成功;1 为 LLM 错误轮/预算超限/空 stdin/--print 无输入/resume 缺失/safe-root 拒绝/system-prompt-file 不可读;2 为 argparse 用法错误。SIGINT/SIGTERM 第一次 set loop.abort(中断当前轮,产 is_meta 消息),第二次 exit(130),Windows 注册 SIGBREAK。预算超限 stderr 打 "Error: Exceeded USD budget" 且 exit 1 —— 比 Kode print 模式 exit 0 更符合脚本语义。**
**深度衍生: 为什么预算超限是 exit 1 而不是 0?** → **exit 0 表示"任务完成",预算超限是"任务未完成",0 会让 CI 误判成功。退出码纪律:1(运行时未完成)与 2(用法错误)可区分,调用方不用解析输出文本判断成败。**
**广度衍生: 对比 LangGraph 等 agent 框架的终止语义?** → **多数框架只暴露"循环结束"不编码类别,调用方要解析文本。CodeSage 把终止类别编码进三层:is_meta 消息(人可读)、RunSummary(--output-format json 机器可读)、退出码(进程语义)—— 这是 harness 产品化与 demo 脚本的分水岭。**

**Q: --resume 怎么实现?历史喂给模型吗?**
**A: --resume 用 most_recent_session 找最新会话(--session-id 精确找),在 sessions 目录扫 JSONL;命中后 _print_history_summary 打印最近 10 条供人回顾,然后开全新 Session(project_key 沿用,项目目录隔离)。刻意降级:历史不进模型上下文 —— 完整 resume 属阶段 12。**
**深度衍生: 为什么"resume 会话"与"继续对话"分离?** → **喂历史涉及压缩/裁剪策略(上下文成本、相关性),V1 的 resume 是"项目续跑入口"而非"上下文续接" —— 先解决会话文件发现与展示,再谈语义续接;同时 session 只持久化消息流(阶段 04 JSONL),工具状态天然不续接,新会话从干净注册表开始。**
**广度衍生: 对比 ChatGPT 会话续接,agent 会话 resume 的独特难点?** → **对话续接只需消息历史;agent 会话还含工具状态(文件变更、权限记忆),续接需决定哪些状态可复用、哪些必须重置。CodeSage 选择"全部重置只留项目上下文",这是降级 resume 背后的安全取舍:避免陈旧工具状态污染新上下文。**

**Q: remember(r) 写进什么?为什么是精确粒度?**
**A: request_permission 的 (r)emember 调 save_approval 写 settings.local.json 的 permissions.allow,规则由 build_rule_string 生成:Bash → `Bash(<命令前 80 字符,空白归一>)`,Edit/Write → `Edit(<父目录>/**)`,其余 → 裸工具名。注意写保护路径不会被 remember 放行:即使记住了 allow 规则,写保护检查在规则之前(硬地板),下次仍 ask。**
**深度衍生: 为什么 Bash 只记前 80 字符?** → **批准的最小单元是"这一条命令"不是"所有命令":完整命令精确匹配 Bash(<cmd>),加前缀通配 Bash(<prefix>*) 支持近似。80 字符截断防命令无限长撑爆规则文件,空白归一使等价命令(多余空格)也能命中 —— 精度与可用性的平衡点。**
**广度衍生: 这个机制对应权限持久化的什么通用模式?** → **"用户一次批准 → 精确持久化 → 下次自动匹配"与 known_hosts、sudo 时间戳缓存同一模式;区别在粒度:known_hosts 记主机指纹(精确到实体),CodeSage 记命令/路径模式 —— 越精确越安全,这是批次 3 P3(remember 落精确粒度)的核心。**
