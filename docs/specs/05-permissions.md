# Spec: 阶段 05 — 权限引擎

> 分支:`feat/05-permissions`。依据主规格 `docs/specs/codesage.md`(阶段 05)。

## Objective

权限引擎是 harness 的安全门:决定每次工具调用是 allow/ask/deny,并留下审计记录。这是 V1 验收的一部分(权限三态生效),也是未来安全领域适配的核心(审计钩子第一天就带)。

## 对照保留清单

- #5 决策链完整顺序:归一化模式 → 显式规则 → 文件路径规则 → needs_permissions 自声明 → 模式后处理
- #6 deny > ask > allow;写保护路径直接 requiresExplicitApproval
- #18 规则存 settings(阶段 01 的 permissions 字段),不读 AGENTS.md
- 审计钩子(项目意图:安全适配的基础)

## 范围

**做**:
1. `PermissionMode`(plan/default/yolo)+ 只读工具集合
2. 决策链 `evaluate_tool_use`(完整顺序,见下)
3. 规则匹配:工具名精确/前缀(`Skill(foo:*)` 类)/通配(MCP `mcp__*__*`);路径规则(gitignore 语义简化:前缀 + fnmatch + symlink 展开)
4. 写保护路径(.git/.ssh/.codesage/settings 等)与敏感路径
5. **审计钩子**:AuditSink 接口 + 事件(dataclass),可替换实现
6. 规则持久化:批准后写 settings.local.json 的 permissions.allow

**不做**:hooks 汇入(阶段 09);Bash 命令级规则(逐子命令解析,阶段 16);沙箱联动(阶段 16);会话级规则 UI(阶段 07)。

## 决策链(实现顺序)

```
1. 工具名归一化
2. 系统白名单(内部工具:AskUser/TodoWrite 等) → allow
3. 显式规则(settings.permissions.allow/deny/ask,工具名级) → deny > ask > allow
4. 文件工具路径规则(路径级 allow/deny/ask,gitignore 语义) → deny > ask > allow
5. 写保护路径(即使 allow 规则命中) → requiresExplicitApproval(ask)
6. tool.needs_permissions(input) == False → allow(只读自声明)
7. 模式后处理:
   - plan 且非只读工具 → deny
   - yolo 且非 requiresExplicitApproval → allow
   - 默认 → ask
8. 审计事件(每次决策)
```

## 项目结构(本阶段新建)

```
codesage/codesage/permissions/
  __init__.py
  engine.py        # PermissionEngine + evaluate_tool_use
  modes.py         # PermissionMode + READ_ONLY_TOOLS
  rules.py         # 工具名/路径规则匹配
  paths.py         # 写保护路径 + symlink 展开
  audit.py         # ToolAuditEvent + AuditSink
  store.py         # 规则读取 + save_approval
tests/permissions/
  test_engine.py
  test_rules.py
  test_paths.py
  test_audit.py
  test_store.py
```

## Commands

```bash
pytest tests/permissions/ -q
```

## Testing Strategy

- 决策链矩阵:每个分支一条断言(白名单/规则三态/路径/写保护/自声明/三模式/默认)
- deny 不可被 yolo 绕过(关键安全性质)
- 审计:每次决策恰好一个事件,内容完整

## Boundaries

- **Always**: 决策必须过审计;deny 优先性不可破坏;路径比较用 resolve(防 symlink 绕过)
- **Ask first**: 改决策链顺序;放宽写保护
- **Never**: 读取 AGENTS.md 作为权限来源;跳过审计;默认 allow 未知工具

## Success Criteria

- [ ] 决策链矩阵全测覆盖,deny 不可被 yolo 绕过
- [ ] 审计钩子:每决策一事件,可替换 sink
- [ ] 规则持久化 roundtrip(批准 → settings.local.json → 下次生效)
- [ ] 全量单测绿
