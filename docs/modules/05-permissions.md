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
