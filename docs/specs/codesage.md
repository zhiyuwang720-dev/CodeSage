# Spec: CodeSage — Python Harness 框架

> 来源:`docs/intent/codesage.md`(访谈确认)+ `docs/ideas/codesage.md`(方向收敛)+ Kode-CLI 深度阅读(7 领域并行勘察)。
> 本文档为项目主规格,每个阶段在各自分支上补充阶段级规格。

## Objective

用 Python 实现一个类 Claude Code / Kode-CLI 的 Harness 框架(CodeSage),让用户:

1. **学习**:通过分阶段复刻,彻底理解 harness 的每个模块
2. **未来改造**:完成后用于大型项目编写和安全领域适配(权限、审计、沙箱)

**重要:本项目重新开始。** 现有 `backend/app/service/` 代码是探索产物,不作为新架构的组成部分;实现时可参考其思路与代码,但新架构从零设计。

## Tech Stack

| 项 | 选择 | 说明 |
|---|---|---|
| 语言 | Python ≥3.11 + asyncio | |
| HTTP | httpx | LLM 传输、WebFetch、MCP |
| 类型/校验 | pydantic | zod 的对应物(config 已有使用经验) |
| 测试 | pytest + pytest-asyncio | `asyncio_mode=auto` |
| 协议 | OpenAI 兼容为主,Anthropic 原生为辅 | |
| 参考实现 | Kode-CLI(本机 `/e/Mac/github/Kode-CLI`) | 功能地图 + 关键设计清单(见下) |

依赖纪律:不新增框架级依赖,除非阶段规格论证。

## Kode-CLI 关键设计保留清单(每阶段不得遗漏)

从 7 领域勘察提炼,各阶段实现时对照:

1. **主循环 = 递归 async generator**,Message 流是唯一信息通道;终止条件五类(终答/hooks 阻断/中断/thinking-only 超限/maxTurns·maxBudget)
2. **工具失败不抛异常** → `is_error: true` tool_result 交给模型自愈;唯一硬异常是 maxTurns/maxBudget
3. **工具并发由 `isConcurrencySafe` 声明驱动**,非安全工具 = 顺序屏障;一个工具出错 → sibling 收 `<tool_use_error>`
4. **中断走 AbortSignal 三检查点**(LLM 调用后 / 工具队列 / hooks),不 throw
5. **权限决策链完整顺序**:归一化模式 → dontAsk 预拒 → bypass(仅安全地板)→ `needsPermissions()` 自声明 → 规则合并 → 按工具名分发 → 后处理(yolo 自动 allow 但不绕过 requiresExplicitApproval)
6. **deny > ask > allow**;`.git/.ssh/settings` 等写保护路径直接 requiresExplicitApproval
7. **复合 Bash 命令逐子命令判定**(任一 deny 即整体 deny);`cd`+写/重定向 → 强制 ask
8. **Bash 纵深 8 层**:权限 → Windows 策略 → 破坏性守卫 → 沙箱计划 → LLM 意图闸门(fail-closed) → validateInput → 运行时真超时/kill → 执行后自愈(cwd 重置/文件新鲜度)
9. **沙箱约束从权限规则推导**;网络沙箱 = HTTP CONNECT + SOCKS5 代理网关
10. **hooks 可改写权限决策**(PreToolUse permissionDecision 汇入决策链)与输入(updatedInput)
11. **模型指针** main/task/compact/quick + 辅助请求失败回退 main;自管重试(尊重 retry-after),禁用 SDK 重试
12. **内部消息形状统一**(Anthropic 式 content blocks 为规范契约),OpenAI 侧转换;token 归一化只在边界一次
13. **两级压缩**:auto-compact(LLM 摘要,compact 指针)+ micro-compact(超大工具结果 >40k tokens → 400 字符预览 + 落盘)
14. **持久化 = JSON 文件 + tmp+rename 原子写 + 文件锁**(无数据库);会话 = append-only JSONL,summary 挂 leafUuid,恢复保摘要前 2 条 user 消息
15. **子代理与主代理共用引擎**,进程内嵌套 generator 即通信;`SUBAGENT_DISALLOWED_TOOL_NAMES` 禁递归工具;forkContext 读父会话、resume 从转录缓存恢复
16. **MCP 工具强制 `needsPermissions()=true`**(服务器描述不可信);工具名 `mcp__server__tool`;listChanged 版本号驱动缓存失效;OAuth PKCE 远程鉴权
17. **记忆 = JSONL 事件溯源 + 保守写入**(只提取显式标记句子)+ 本地词法检索 + 注入时标注「不可信数据」
18. **配置双轨**:settings 三层(user/project/local 覆盖)+ 全局配置;AGENTS.md 仅作上下文注入,**不参与权限**
19. **AGENTS.md 支持**:git root → cwd 逐层收集 + override 文件 + 字节预算截断(32KB)
20. **system-reminder 服务**:状态变化/安全/长会话提醒注入,每会话上限 10 条

> **注记(2026-08,refactor/engine-config)**:主循环配置显式化为 `AgentLoopConfig`、per-run 可变状态收拢为 `RunState`(engine/loop.py),单次运行外壳 `AgentSession.submit` 与 `RunSummary`(自 cli 迁入,engine/session.py)—— 对齐 CC QueryEngineConfig / State / submitMessage 三层设计;渲染路径不变。详见 `docs/modules/06-engine.md` 尾部决策记录。

## Commands

```bash
# 测试(项目根,pyproject 与源码同在 codesage/ 根)
pytest tests/ -q
# 运行 harness(阶段 07 后)
python -m codesage.cli
# 集成测试(有 key 时运行,无 key 自动 skip)
DEEPSEEK_API_KEY=xxx pytest tests/ -q
```

## Project Structure(从零,阶段演化)

```
codesage/                      # 项目根(现有 backend/ 不并入)
  pyproject.toml               # 项目名 codesage
  codesage/
    config/      # 01 配置系统:settings 三层 + 全局配置
    ai/          # 02 LLM 客户端:types/retry/cost/vcr/client + adapters/
    tools/       # 03 工具契约 + 注册 + 内置工具
      base.py    #   契约层(Tool/ToolResult/...)
      registry.py
      builtin/   #   内置工具按类别子包,每工具一文件
        filesystem/{ls,read,write,edit}.py + _common.py
        search/{glob,grep}.py + _common.py
        shell/bash.py
    core/        # 04 消息与会话;10 压缩;11 任务;12 会话生命周期;18 记忆
    permissions/ # 05 权限引擎
    engine/      # 06 主循环(Agent Runtime)
    cli/         # 07 REPL
    context/     # 08 上下文管理(AGENTS.md)
    hooks/       # 09 钩子系统
    agents/      # 13 子代理(定义解析 + 多代理扩展;Task 工具本体已落 11 core/tasks/ + tools/builtin/interaction)
    skills/      # 14 技能系统(SKILL.md 定义 + 加载/注册表 + 提示词管道 + 双路径调用)
    mcp/         # 16 MCP 客户端
    safety/      # 17 Bash 安全纵深(LLM 闸门 + 沙箱计划)
  tests/                     # 镜像源码:每个模块一个子目录
    config/  ai/  tools/  ...
  docs/
    intent/  ideas/  specs/  modules/     # modules/ = 每阶段理解文档
```

### 目录规划规范(生产级,所有阶段遵守)

1. **每模块一个包**(`codesage/<module>/`),包内按职责分层:
   - 契约层(`base.py` / `types.py` / `contract.py`)—— 类型与接口,不依赖实现
   - 实现层 —— 按类别子包(`builtin/`、`adapters/`),**每工具/每适配器一个文件**,文件名即工具名(`ls.py`、`read.py`、`openai_compatible.py`)
   - 类别共享辅助放 `_common.py`(下划线前缀,不导出)
   - 入口层(`registry.py` / `client.py` / `factory.py`)—— 装配与对外 API
2. **包级 `__init__.py` 显式导出**公共 API,深路径导入只允许在模块内部和测试里
3. **tests 镜像源码**:`tests/<module>/test_<file>.py`,一个源码文件对应一个测试文件(每工具一个测试文件)
4. **命名**:文件/模块小写下划线;类 PascalCase;测试函数 `test_<行为>`

## Code Style

以类型为先:pydantic 模型 + dataclass、全量类型注解、模块 docstring、异常携带上下文。故意简化处标 `ponytail:` 注释说明上限与升级路径。风格样例:

```python
class PermissionDecision(BaseModel):
    allowed: bool
    mode: Literal["allow", "ask", "deny"] = "deny"
    reason: str | None = None
    source: str | None = None
```

约定:异步优先;函数/类/docstring 一句说明意图;阶段新代码风格与既有阶段一致。

## Testing Strategy

- pytest + pytest-asyncio(`asyncio_mode=auto`),`tests/`
- 每阶段:核心逻辑单测(权限决策、队列状态机、循环终止、压缩边界)
- LLM 集成测试:无 key 自动 skip(conftest 模式)
- 复杂逻辑保留一个可运行自检(`__main__` demo 或单个 test 文件)
- **VCR 模式**(阶段 02 引入):LLM 调用录制/回放,CI 可离线跑

## Boundaries

- **Always**: 每阶段从读对应阶段规格开始;合并前全量测试绿;文档与代码同 PR;文档中文
- **Ask first**: 新增依赖;调整阶段顺序或模块边界;改 pyproject 元数据;引入 lint/格式化工具
- **Never**: 直接提交 master(只能合并);删除失败测试;提交 API key/密钥;修改 Kode 参考项目

## 阶段路线图(分支:feat/0N-xxx)

V1 = 阶段 01–07(最小闭环:REPL + 单模型 + 核心工具 + 权限门控,端到端完成小任务)

| # | 分支 | 模块 | 内容(对照保留清单) | 交付 | 状态 |
|---|---|---|---|---|---|
| 01 | `feat/01-config` | 配置系统 | settings 三层 + 全局配置 + AGENTS.md 发现 + BOM/symlink/mode/降级 | 配置 + 单测 | ✅ 已交付 + 强化 |
| 02 | `feat/02-ai` | LLM 客户端 | 双 adapter、流式、重试、成本、模型指针、VCR + 传输错误包装/取消/截断处理 | 适配器 + 单测 | ✅ 已交付 + 强化 |
| 03 | `feat/03-tools` | 工具契约与内置工具 | Tool 契约、注册表、**12 工具**(LS/Read/Write/Edit/Glob/Grep/Bash/TaskOutput/TaskStop/TodoWrite/WebFetch/AskUserQuestion 规划中)+ 陈旧性校验/守卫/后台 | 工具 + 单测 | ✅ 已交付 + 强化 |
| 04 | `feat/04-core` | 消息与会话 | Message 类型、normalize(toolResultsFirst)、会话 JSONL + 项目作用域 | 消息模型 + 单测 | ✅ 已交付 + 强化 |
| 05 | `feat/05-permissions` | 权限引擎 | 10 步决策链、Bash 静态分析、规则 Tool(content) 语义、工作目录约束、写保护、审计 | 决策链 + 单测 | ✅ 已交付 + 强化 |
| 06 | `feat/06-engine` | 引擎主循环 | while 迭代(非递归)、ToolUseQueue、错误自愈、abort、thinking-only 重试、结果落盘 | 循环 + 单测 | ✅ 已交付 + 强化 |
| 07 | `feat/07-cli` | CLI REPL | 交互/单次双模式、权限询问、信号、--print/--resume/--safe/--json/--budget | REPL + 验收 | ✅ 已交付 + 强化 |
| 08 | `feat/08-context` | 上下文管理 | AGENTS.md 收集/截断(#19)、system prompt 分层组装、system-reminder(#20) | 上下文 + 单测 |
| 09 | `feat/09-hooks` | 钩子系统 | 八事件(SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop/PreCompact/PostCompact/Notification)、命令+提示+HTTP 三执行体、`if` 条件过滤、JSON 结果解析 | 钩子 + 单测 | ✅ 已交付 |
| 10 | `feat/10-compact` | 上下文压缩 | token 预算、auto-compact(LLM 摘要)+ micro-compact(#13) | 压缩 + 单测 |
| 11 | `feat/11-tasks` | 任务系统 | Task CRUD、blocks/blockedBy 依赖环检测、todo | 任务 + 单测 |
| 12 | `feat/12-session` | 会话生命周期 | fork/continue/resume、sidechain 日志、归档、会话选择器支持 | 会话 + 单测 |
| 13 | `feat/13-subagents` | 子代理 | agent 定义(frontmatter 解析 + 优先级合并 + 内建三类型)、Agent 工具(前台嵌套/后台/forkContext/worktree 隔离)、SendMessage 队友通信、任务扩展(共享列表/owner 归属)、禁递归 | 子代理 + 单测 |
| 14 | `feat/14-skills` | 技能系统 | 技能定义(SKILL.md frontmatter 白名单 + 内置>管理>用户>项目优先级合并 + 内置层机制,内置恒不可被覆盖)、加载(目录扫描/realpath 去重/lru 缓存)、提示词管道(参数/环境变量/内联 shell)、双路径调用(斜杠命令兜底 + SkillTool 自动触发)、allowed_tools 权限联动(只豁免默认 ask)、availableSkills 列表注入、压缩后技能恢复 | 技能 + 单测 |
| 15 | `feat/15-mcp` | MCP 客户端 | stdio/HTTP 传输、工具注册(mcp__ 命名 + needsPermissions 强制 #16)、resources、OAuth | MCP + 单测 |
| 16 | `feat/16-bash-safety` | Bash 安全纵深 | 破坏性守卫、LLM 意图闸门(fail-closed #8)、沙箱计划(#9,Windows 降级为文档 + Linux 预留) | 安全 + 单测 |
| 17 | `feat/17-memory` | 记忆系统 | JSONL 事件溯源、保守提取、本地词法检索、注入标注(#17) | 记忆 + 单测 |
| 18 | `feat/18-multimodel` | 多模型编排 | 专家模型、辅助回退增强、上下文感知切换 | 编排 + 单测 |
| 19 | `feat/19-plugins` | 热插拔注册层 | 收尾:模块注册表、≥2 个真实实现后设计接口、插件化工具/技能/MCP 统一入口 | 注册层 + 单测 |

每阶段交付:`[代码] + docs/modules/0N-*.md(理解文档)+ 单测全绿 + 合并回 master`。
阶段规格在该分支细化(六项核心区 + 完成标准 + 对照保留清单)。

## V1 生产级强化记录(2026-08-05,三轮修复)

V1(01–07)交付后经 7 代理对照 Kode 审查(功能级 + 文件级两轮),确认未达生产级标准,执行三轮修复(共 ~4200 行,测试 170 → 337):

| 轮次 | 范围 | 关键修复 |
|---|---|---|
| 批次 1 | ai/core/config/tools | 传输错误包装(重试复活)、stream 成本累计(max_budget 死代码)、Edit/Write 陈旧性校验、Bash 守卫/cd 限制、后台任务、BOM/symlink/mode、normalize toolResultsFirst、OpenAI 转换丢 text bug |
| 批次 2 | permissions/engine/cli | **工作目录约束(堵 yolo 任意写)**、Bash 静态分析、规则 ! 否定、thinking-only 重试、validate_input 接线、超大结果落盘、stdin/--resume/--safe/退出码 |
| 批次 3 | 文件级 A 类 19 项 | 规则 Tool(content) 语义(路径约束恢复)、Bash 精确规则 + 子命令级、remember 精确粒度、Windows 守卫、取消贯穿 HTTP、截断流丢弃 tool_use、Grep rg 化、TodoWrite/WebFetch(SSRF)、cli 7 项 |

**结论**:V1 现达到生产级标准(差距分析见 `docs/gap-analysis-v1.md` + `docs/gap-analysis-file-level.md`)。Kode 剩余功能全部映射后续阶段(08–19)或有明确 C 类理由(daemon 域/ant 内部/UI 框架/兼容层)。

## 假设清单(默认成立,可修正)

1. **项目重开**:现有 `backend/app/service/` 代码不并入新架构,仅作参考素材
2. **项目名** `codesage`,新目录 `codesage/`(与现有 backend/ 并存,阶段 07 前可迁移清理)
3. **分支命名**:`feat/0N-xxx`,master 只收合并
4. **V1 = 阶段 01–07**,闭环验收在 07
5. **文档中文**,阶段文档放 `docs/modules/`
6. **Python 生态**:pydantic 替代 zod、httpx 替代 undici;沙箱(阶段 16)Windows 上不可用则文档化 + 预留
7. **协议层不单列阶段**:stream-json SDK 模式/daemon 控制面属于 out-of-scope 的 server/daemon,消息类型在阶段 04 自然承载

## Success Criteria

- [ ] V1 闭环:REPL 中「创建 docs/hello.md」类任务端到端完成,权限 allow/ask/deny 生效,审计钩子有记录
- [ ] 阶段 01–19 每阶段:模块完成 + 理解文档 + 单测全绿 + master 干净合并
- [ ] 热插拔:同一类模块存在 ≥2 个实现,注册层切换无代码侵入
- [ ] 未来适配预留:权限运行时审计事件可被安全领域逻辑消费(无需改核心)

## Open Questions

- 阶段 06 system prompt 骨架的深度(完整分层 vs 骨架)—— 阶段 06 定
- 沙箱在 Windows 的策略(完全跳过 vs 文档化)—— 阶段 16 定
- 阶段 11 任务系统与阶段 04 会话的边界(任务持久化层级)—— 阶段 11 衔接时定
