# 阶段 05 — 权限引擎理解文档

> 分支 `feat/05-permissions`,规格见 `docs/specs/05-permissions.md`。

## 模块职责

权限引擎是 harness 的安全门:对每次工具调用给出 **allow / ask / deny** 三态决策,并留下不可跳过的审计记录。它是 V1 验收的一部分(权限三态生效),也是未来安全领域适配的核心 —— **审计钩子第一天就带**,安全领域的威胁模型/沙箱逻辑可以直接消费审计事件,不用改引擎。

## 决策链(完整顺序)

```
1. 系统白名单(内部工具:AskUser/TodoWrite/...)→ allow
2. 显式规则(settings.permissions.allow/deny/ask)→ deny > ask > allow
3. 文件工具路径规则(同一步骤,路径级 deny > ask > allow)
4. 写保护路径(.git/.ssh/.env/settings 等)→ 强制 ask(即使 allow 规则命中)
5. 敏感读路径(.env/密钥文件)→ 强制 ask
6. 工具自声明 needs_permissions()==False → allow(只读工具)
7. 模式后处理:plan 拦写 / explicit-approval 工具强制 ask / yolo 自动放行
8. 默认 → ask(未知工具永不默认放行)
9. 审计事件(每次决策恰好一条)
```

**关键安全性质(有专门测试)**:
- **deny 不可被任何模式绕过** —— yolo 只自动放行「本会 ask」的结果
- **写保护优先于 allow 规则** —— `.git/config` 即使有 allow 规则也要显式批准
- **默认 ask** —— 没有规则的工具绝不默认放行

## 关键设计决策

### 1. 规则模型:settings 单一来源(设计笔记 #18)

规则只来自 `settings.permissions.{allow,deny,ask}`(阶段 01 的字段)+ 会话级规则(阶段 12 预留)。**AGENTS.md 永不参与权限** —— 项目指令是上下文,不是安全边界(模型可被提示注入,权限规则不能)。

### 2. 规则匹配:工具名 + 路径双轨

- 工具名规则:精确、glob(`mcp__*__*` 为阶段 15 预留)、`Skill(foo:*)` 前缀形式
- 路径规则:绝对路径前缀、`/**`(递归)、`/*`(单层)、glob —— gitignore 语义的简化版;规则与路径统一正斜杠比较(Windows 安全)

### 3. 写保护:路径组件黑名单 + 敏感文件

`.git/.ssh/.codesage` 组件、settings/env 类文件名、会话/记忆数据目录 —— 命中即不可写。**symlink 展开**(resolve)防 `symlink → .git` 绕过。

### 4. 审计:事件 + 可替换 sink

`ToolAuditEvent`(工具名/决策/原因/来源/模式/输入摘要)每次决策发一条。sink 是 Protocol(可替换):默认 `JsonlAuditSink`(append-only,与存储层同一套路),未来安全适配可以换成告警/策略消费器。**审计摘要只含路径,不含内容与密钥**(有专门测试断言)。

### 5. 只读工具自声明 + 敏感读兜底

内置只读工具(LS/Read/Glob/Grep)`needs_permissions()→False`,日常读不打扰;但**读 `.env` 等敏感文件仍强制 ask** —— 自声明只管「免打扰」,敏感兜底在引擎(阶段 03 的契约声明 + 本阶段的兜底检查,缺一不可)。

## 与 Kode 的对照

| CodeSage | Kode | 差异 |
|---|---|---|
| 三模式(plan/default/yolo) | 七模式(yolo/cautious/default/acceptEdits/plan/bypassPermissions/dontAsk) | 阶段 07 的 CLI 先暴露三模式;其余按需加 |
| 工具名 + 路径规则 | 同 + Bash 命令级(逐子命令) | Bash 命令解析阶段 16 |
| 审计 JSONL sink | 权限 UI + 会话授权存储 | 无 UI 的形态:审计先行(安全适配需要) |
| 无 hooks 汇入 | PreToolUse permissionDecision 汇入决策链 | 阶段 09 接上 |

## 已知简化(ponytail)

- 路径规则无 gitignore 的 negation(`!pattern`)—— 需要时加
- Bash 无命令级规则(只按工具名)—— 阶段 16 的逐子命令判定
- 无会话级「本次批准」内存态(阶段 12 的 session_permissions 参数已预留)

## 完成标准(对照规格)

- [x] 决策链矩阵 21 项测试全测覆盖,deny 不可被 yolo 绕过
- [x] 审计钩子:每决策一事件、可替换 sink、摘要无内容
- [x] 规则持久化 roundtrip(批准 → settings.local.json → 重载)
- [x] 141 项全量单测绿

## 阶段衔接

- 阶段 06(engine):`evaluate_tool_use` 是工具执行链的一环(校验 → 权限 → 执行)
- 阶段 07(CLI):ask 决策 → 终端确认交互;批准 → `save_approval`
- 阶段 09(hooks):PreToolUse 钩子产出汇入决策链
- 阶段 16(bash-safety):命令级规则 + 沙箱联动
- 安全适配(未来):审计事件消费、威胁模型、策略引擎

## 生产级强化(2026-08-05)

三轮修复(对照 Kode 审查,测试 170 → 337):

**修复内容**(批次 2 permissions + 批次 3 P1-P4):
- [高] **工作目录约束**(项目根 + 附加目录,目录外读写强制 ask + 显式批准)—— yolo 任意写漏洞堵死
- [高] 规则字符串解析 Tool(content) 语义(P1):`allow:["Read(/abs/**)"]` 不再整串被当工具名 fnmatch,路径约束恢复且不再先于写保护
- [高] Bash 精确规则 + 子命令级评估(P2):拆分子命令 + 重定向目标 + rm 临界目标 + 注入模式
- [高] remember 落精确粒度(P3):`Bash(<cmd>)`/`Edit(<path>/**)` 精确 key,不再记住一条放行任意命令
- [高] Windows 路径守卫补全(P4):尾部点/空格、NTFS ADS、`\\?\` 前缀
- [高] 规则源分层(user/project/local/session)+ gitignore 否定语义(`!` 撤销)+ 会话内存授权态
- [高] 写保护清单补全(dotfiles/.vscode/.idea/UNC/Windows 可疑路径)
- [低] 删 engine.py 死代码;Skill 移出 SYSTEM_TOOLS

**文件级判定**:
- A 类(已实现):P1-P4 安全 4 项全落地(文件级唯一 P0 集中地)
- B 类(映射阶段 X):hooks(09) PreToolUse 汇入决策链、Bash 纵深(16)
- C 类(理由):daemon 域(goals/supervisor/runs 跨进程)、oauth 登录

**现状**:及格(接近差) → 良好。yolo 任意写漏洞已封,规则解析/精确粒度/Windows 守卫补齐,审计链不变;hooks 汇入与 Bash 纵深留给 09/16。

## 设计决策剖析

### 为什么这么设计

1. **决策链线性固定,而非策略分发** —— 9 步顺序(系统白名单 → Bash 分析 → 显式规则 → 写保护 → 工作目录 → 敏感读 → 自声明 → 模式后处理 → 默认 ask)写成函数内一条顺序链。动机:安全属性要可枚举、可测试 —— 21 项矩阵测试就是对着每一步写的,任何一步提前 return 的后果都可预测。
2. **deny 绝对 + 写保护硬地板** —— deny 在模式判断之前返回,任何模式不可绕过;写保护检查放在 allow 规则之前,显式 allow 也写不进 `.git`。动机:两个最危险的绕过面(模式放行、规则覆盖)从顺序上就堵死,而不是靠"记得检查"。
3. **工作目录约束为绝对约束** —— 文件工具目标 resolve 后必须在某 working_dir 内,越界即 ask(requires_explicit_approval),yolo 不自动放行;但显式 allow 规则仍可赢(用户可精确授权)。动机:修复"模型把文件写到项目外"的真实攻击面(批次 2 的 yolo 任意写漏洞)。
4. **Bash 子命令级静态分析** —— 拆分子命令 + shlex 分词后逐子命令判定(rm 临界目标 deny、越界写 ask、注入模式 ask)。动机:静态分析确定性、零延迟、可单测;承认天花板(不是 shell 解析器),阶段 16 的 LLM 意图闸门兜底。
5. **审计与决策同构、sink 可替换** —— 每次决策恰好一条 ToolAuditEvent,摘要只含路径;sink 是 Protocol,默认 JSONL。动机:安全域适配(威胁模型/告警)第一天就有消费通道,不用回头改引擎。

### 设计原则

- **fail-closed**:默认 ask,未知工具永不默认放行
- **deny 绝对**:任何模式不可绕过 deny(yolo 只自动放行"本会 ask"项)
- **最小特权**:只读工具自声明免打扰,敏感读兜底仍在引擎;remember 记精确粒度而非整工具
- **安全默认**:显式批准项(yolo 也不放)与写保护、工作目录约束互为兜底
- **规则与代码分离**:AGENTS.md 永不参与权限 —— 提示可被注入,规则不能
- **跨平台同语义**:正斜杠比较 + Windows 守卫(尾部点空格 / NTFS ADS / `\\?\`)补全

### 优点

- 决策链单点修改:加一层规则只动 `evaluate_tool_use`,测试矩阵同步扩充
- 规则解析(P1 修复)后 `allow:["Read(/abs/**)"]` 路径约束真正生效,且不再先于写保护
- remember 精确粒度:`Bash(<cmd>)` / `Edit(<dir>/**)`,不再"批准一次放行任意命令"
- 写保护清单覆盖 dotfiles/.vscode/.idea/UNC/Windows 可疑路径,防绕过面枚举完备
- 审计与引擎解耦:未来换告警 sink 只改装配根,引擎零改动

### 为什么不选用别的技术方案

| 备选方案 | 为什么不选 |
|---|---|
| 完整 gitignore 语义 | 权限需要"工具名 + 路径 + 内容"三维匹配,gitignore 是纯路径视角;现有简化版(前缀、`/**`、`/*`、fnmatch、`!` 否定)约 100 行覆盖需要的子集,源码 ponytail 注释明示"简化 last-wins-by-negation" |
| LLM 做权限闸门 | 静态规则确定性、可测、零成本零延迟;LLM 可被提示注入,不能当安全边界 —— 只在阶段 16 作 Bash 意图兜底 |
| 完整 shell 解析器 | 平台差异(POSIX/Windows cmd)是维护无底洞;shlex + 引号状态机够用,且所有分析方向保守(不确定即 ask/deny) |
| 权限判断放工具内 | 决策与审计必须在引擎单点(工具契约只自声明 needs_permissions)—— 否则每个工具各自为政,审计链断裂 |
| 会话级"本次批准"全内存态 | 只预留 session_permissions 参数(阶段 12 落),避免跨进程/多入口状态不同步 |

## 面试问题整理

### 技术点清单

决策链顺序与 deny 绝对性 / 规则模型(工具名-路径-Tool(content) 三形态 + `!` 否定)/ 工作目录约束与路径安全(写保护、symlink、Windows 守卫)/ Bash 子命令级静态分析 / 审计事件与可替换 sink

### 面试问题与答案

**Q: 权限引擎的决策顺序是什么?deny 能被 yolo 绕过吗?**
**A: 顺序:系统白名单 → Bash 命令分析 → 显式规则(deny > ask > 写保护 > allow)→ 工作目录约束 → 敏感读 → needs_permissions 自声明 → 模式后处理(plan 拦写 / yolo 自动放行)→ 默认 ask。deny 是绝对的:yolo 只自动放行"本会 ask"的项,Bash 在 REQUIRES_EXPLICIT_APPROVAL 里(任何模式不自动放行);deny 分支在任何模式判断之前返回,无法绕过,21 项矩阵测试锁定该性质。**
**深度衍生: yolo 能自动放行工作目录外的写吗?** → **不能。工作目录约束(第 5 步)产出 requires_explicit_approval=True,而 yolo 的自动放行(第 8 步)只处理普通 ask —— 显式批准项不会走到 yolo 分支,这就是"yolo 任意写漏洞"的修复。但显式 allow 规则(第 4 步)仍优先,用户可用规则精确授权。**
**广度衍生: 与 Claude Code 的 PreToolUse 权限模型差异在哪?** → **Claude Code 是七模式 + hooks 汇入决策链,CodeSage 是三模式 + 固定顺序链(hooks 汇入是阶段 09 规划)。相同:deny 绝对、默认 ask、allow 不能覆盖写保护。差异在 Bash:CodeSage 用静态分析子命令,Claude Code 是逐子命令规则 + LLM 辅助。**

**Q: 规则有哪几种形态?`Read(/abs/**)` 解析成什么?**
**A: 三种:裸工具名(支持 glob)、裸路径(前缀/`/**`/`/*`/fnmatch)、`ToolName(content)`。parse_rule 先把内容形态拆成 (工具名, 内容):文件规则按读写集合分组(Read/LS/Glob/Grep vs Write/Edit),内容解析为路径规则;Bash(content) 是命令模式走 bash_rules_match;其余工具退回工具名级。批次 2 的 P1 修复就是:此前 `Read(/abs/**)` 整串被 fnmatch 当工具名,路径约束失效且先于写保护。**
**深度衍生: `!` 规则与 gitignore 的 negation 有何异同?** → **match_first 遇 `!rule` 且内部规则命中时返回 None,撤销之前所有匹配 —— 会话级规则常用它撤销 settings 规则。实现是"后置否定撤销"简化版(ponytail 注释明示),不支持 gitignore 的目录尾斜杠、双星等完整语义。**
**广度衍生: 为什么不直接复用 gitignore 库?** → **gitignore 是"仓库文件视角",权限是"工具调用视角"(工具名 + 路径 + 内容三维);且规则来源多层(settings/session/local),合并带 `!` 撤销语义 —— 库套不上这个模型。自研子集 + 注释标注天花板,需要完整语义时在 match_first 处换实现即可,接口不变。**

**Q: 写保护路径怎么判定?symlink 绕过怎么防?**
**A: is_write_protected 先拒明显危险形态:UNC/`\\?\` 前缀、NTFS 备用数据流(路径第二个冒号)、`..` 遍历段;然后 resolve() 展开 symlink,对展开后的路径逐层检查三张清单:组件黑名单(.git/.ssh/.codesage)、文件名(settings.json/.env/.bashrc 等)、目录(sessions/memory/.vscode/.idea),每个 part 再 rstrip(" .") 处理 Windows 尾部点空格。防 symlink 的核心:引擎对所有文件工具目标都先 resolve 再用真实路径比较。**
**深度衍生: 为什么写保护检查放在 allow 规则之前?** → **写保护是硬地板:先查 allow 的话,一条 allow 规则会静默覆盖保护 —— `.git/config` 就能被写。顺序保证"规则不能升级权限到写保护之上",只有用户当面批准(requires_explicit_approval 的 ask)y 一次才放行,而 yolo 也过不了这关。**
**广度衍生: 相比 macOS 优先的 Kode,CodeSage 多防了什么?** → **Windows 专项(批次 3 P4):尾部点/空格、NTFS ADS、`\\?\` 扩展前缀、IP 形式 UNC、IIS 虚拟路径 @SSL@/DavWWWRoot;同时把 Kode 的 isPathInWorkingDirectories 移植为绝对约束。**

**Q: Bash 命令怎么做子命令级判定?**
**A: 三层:split_commands 带引号状态机按 &&/||/;/|/换行切子命令;shlex 分词;逐子命令分析写动词(mv/cp/mkdir 目标、rm/rmdir 临界目标)、重定向目标(>、>>、2>)、sed -i 原地写 —— 目标出工作目录 ask,rm 危险目标(/、~、工作目录自身)deny。注入模式(`$(`、反引号、`${`、`IFS=`、`<<`)在原始文本上检查,命中即 ask,保守优先。**
**深度衍生: `cd /tmp && echo hi > out.txt` 的判定结果与理由?** → **ask:"cd compound with a write operation"。cd 后 cwd 静态不可知,相对路径重定向目标无法判定是否在工作目录内,整体 ask 更安全 —— 这是静态分析承认天花板:阶段 16 的 LLM 意图闸门就是兜底,源码注释明说 "a clever command can always slip through"。**
**广度衍生: 静态分析与沙箱(seccomp/容器)是替代还是互补?** → **互补:静态分析是准入前低成本门(确定性、可测、零运行时开销),沙箱是准入后强制兜底(平台依赖 + 性能成本)。CodeSage 路线:阶段 16 静态规则 + LLM 意图闸门双保险,沙箱留给未来安全域适配 —— 与决策链同逻辑:多层、fail-closed。**

**Q: 审计事件长什么样?为什么摘要只含路径?**
**A: ToolAuditEvent 含工具名、决策(allow/ask/deny)、reason、source(命中规则/模式/写保护等)、mode、input_summary、timestamp。_summarize 只提取 file_path/path/pattern 三个键并截断 200 字符 —— 命令文本、文件内容、密钥一律不进审计(有专门测试)。sink 是 Protocol:JsonlAuditSink(append-only + fsync,与存储层同套路)/ NullAuditSink(测试)。**
**深度衍生: sink 做成可替换的意义是什么?** → **审计的消费者是未来:威胁模型、告警、策略引擎。Protocol 边界下,换 sink 只改装配根(assemble.py 的 PermissionEngine 构造处),引擎零改动 —— 这就是"审计钩子第一天就带"的含义。**
**广度衍生: 审计与阶段 04 会话 JSONL 的异同?** → **同套路(JSONL append + fsync、损坏行跳过)不同通道:会话是对话产物给模型/用户,审计是安全副产品给审计员;会话的 meta 消息会被 normalize 过滤,审计不过滤、每决策恰好一条、只 append 不可删。**
