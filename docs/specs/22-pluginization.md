# 阶段 22-25 规格:CodeSage 全面插件化改造

> 前置:阶段 21(插件内核,见 `docs/specs/21-plugin-kernel.md`)。本规格是改造路线图的权威地图;
> 每阶段实现前在该分支细化(六项核心区 + 完成标准)。
> 战略背景:`docs/specs/后续设计想法.md`「V2 插件化转向」。

## 1. Context(为什么改造)

现状:13 模块、19,030 行生产代码,`cli/assemble.py` 是构造器 DI 组合根 —— **已经 DI 化,
缺的是「组合层」**:无 manifest/loader、无事件总线、无插件生命周期、会话是「存储即记录」而非「日志为源」。
改造不是重写(契约层不动),是**加一层组合 + 每个模块一个插件文件**。

## 2. 改造深度决策(三档,本路线图执行档位)

| 档位 | 内容 | 工作量 | 决策 |
|---|---|---|---|
| ① 表面插件化 | 内核 + 模块包装:能力经 ctx 注册,装配走 manifest | 2600~3600 行 | **本路线图基线** |
| ② 语义对齐 | ① + 会话事件溯源派生层 + Scope | +1000~1700 行 | **事件溯源派生层采纳**;waterfall/claim 后置 |
| ③ 能力对齐 | ② + sandbox/web/workflow/goal 等 DSH 能力栈 | 不计上限 | **不做**,属新项目按需另排 |

选②的理由:事件溯源派生层与既有 compaction(阶段 08/10)自然衔接,且「模型可见 = 已记日志」
正是阶段 12 不变量的延续;waterfall 中间件升级与 claim/inbox 语义等真有需求时再补
(如做沙箱策略插件化时 pre-execute 水瀑自然需要)。

## 3. 阶段拆分与依赖图

```
21 插件内核 ──→ 22 boot(profile/bundle) ──→ 23 核心 seam 化
                                                  ├──→ 24 会话事件溯源 + Scope ──→ 25 全模块迁移收尾
```

| 阶段 | 分支 | 内容 | 预计增量 |
|---|---|---|---|
| 22 | `feat/22-boot` | Profile/Bundle 组合层:manifest 分层(内置 → 用户 → --patch)、patch last-wins、`dump-tree` 等价命令、`assemble.py` 转 compat shim(双轨并行,测试保绿) | 400~700 |
| 23 | `feat/23-seams` | 核心 seam 化:llm/tools/permissions/hooks 四服务进 ctx(服务定义 + 提供者 + 消费方),assemble 接线改 manifest 行;事件总线接入(hooks 从快照回调升级为 emit 兼容层) | 800~1200 |
| 24 | `feat/24-session-scope` | 会话事件溯源化:SessionEventMap 词汇表、deriveMessages() 派生层、「模型可见=已记日志」运行时断言、fork 语义;Scope:每 agent 隔离 ctx(注册随 agent 销毁回滚) | 700~1100 |
| 25 | `feat/25-finalize` | 全模块迁移收尾:skills/mcp/intel/agents/config 全部插件化、manifest 全量行、assemble.py shim 下线、全量回归 | 400~700 |

合计约 2300~3700 行(内核 1200~1900 另计),5 阶段。

## 4. Seam 化清单(13 模块 → ctx 键 → 插件文件)

| 模块 | ctx 键 | 服务定义(现契约层) | 插件文件 |
|---|---|---|---|
| `ai/` | `ctx.llm` | `LLMClient` | `ai/plugin.py`(注册 client + adapter 注册口) |
| `tools/` | `ctx.tools` | `ToolRegistry` + `Tool` 契约 | `tools/plugin.py`(`get_builtin_tools` 注册) |
| `permissions/` | `ctx.permissions` | `PermissionEngine` | `permissions/plugin.py`(audit sink 注入) |
| `hooks/` | `ctx.hooks` | `HookManager` | `hooks/plugin.py` |
| `engine/` | `ctx.agentLoop` | `AgentLoop` + `AgentLoopConfig` | `engine/plugin.py`(15 构造参数改从 ctx 组装) |
| `core/` | `ctx.sessions` | `Session`(24 升级为事件溯源) | `core/plugin.py` |
| `skills/` | `ctx.skills` | `SkillRegistry` | `skills/plugin.py` |
| `mcp/` | `ctx.mcp` | `McpManager` | `mcp/plugin.py` |
| `intel/` | `ctx.intel` | `CodeIntelligenceService`(可选 seam,fail-open) | `intel/plugin.py` |
| `agents/` | `ctx.agents` | 子代理工厂(新能力:工厂 + scope) | `agents/plugin.py` |
| `config/` | `ctx.settings` | `GlobalConfig`/`load_settings` | `config/plugin.py` + manifest 解析 |
| `cli/` | — | — | `cli/boot.py`(替代 assemble.py) |

## 5. 兼容策略(关键)

1. **assemble.py 转 compat shim 全程保留**:`build_loop()` 照常工作,插件路径并行上线;
   全部迁移完(阶段 25)才删除。既有 21K 行测试几乎不动 —— 这是控制改造风险的最大杠杆。
2. **每阶段合并门**:该阶段单测全绿 + 既有全量回归绿(双轨不破)。
3. **loop.py 独立阶段处理**(23):engine 是 11x 热路径,15 个构造参数改 ctx 查找是机械活但动最热文件,
   先用兼容层保住现有测试,再切 ctx 读取。
4. **Scope 是新能力不是改造**(24):子代理现有 `_agent_name` 隔离,升级为真正的作用域注册。

## 6. 明确不做(本轮)

- waterfall/claim 语义对齐(hooks 升级到 emit 兼容层即止)
- 沙箱接缝、web UI、workflow/goal/jobs、typert 等价物 —— 能力对齐档,按业务需求另排
- HMR/热重载 —— 内核注释已标天花板(ponytail:)

## 7. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| R1 | loop.py 热路径回归 | 23 独立阶段 + 兼容层 + 全量回归兜底 |
| R2 | 事件溯源化破坏 session 兼容 | 24 保持 JSONL 磁盘格式不变(append-only 追加新事件型),读侧加派生层,旧会话可读 |
| R3 | 测试面巨大(21K 行) | shim 保绿 + 每阶段只改本阶段装配层测试 |
| R4 | 双轨期间配置分裂(manifest vs settings) | 22 定义映射规则:settings 读进 `ctx.settings` 服务,manifest 只定「装哪些插件」,两轨职责分离 |
| R5 | 改造期间功能冻结感 | 每阶段交付即可用增量(23 交付后即可用 manifest 换 llm adapter),价值逐步兑现 |

## 8. 每阶段交付标准(通用)

`[代码] + docs/modules/2N-*.md + 该阶段单测绿 + 既有全量回归绿 + 合并回 master`。
阶段规格在该分支细化,对齐主规格目录规范与测试镜像规范。
