# CodeSage PR 审计 Agent — 总体架构规格（v2）

> 状态:修订 · 关联 PRD:`docs/prd/PRD.md` · 调研证据:`docs/reference/` + 四项目源码实测
> 冗余清理依据:`docs/architecture/autocve-冗余与精简分析.md`
> 本文件是总纲,各阶段模块文档见 `01-input.md` ~ `04-product.md`

## 1. 为什么需要本架构(背景)

CodeSage 要做一个 **PR 审计 Agent**:输入是 PR(或 diff),输出是结构化审查评论,用多 Agent 专业化替代单提示审查,并且必须有评测闭环(code-review-benchmark 离线数据集)。

我们面前有四份可复用的代码资产,先行的调研(2026-08-28,全部 `file:line` 证据)结论:

| 项目 | 规模 | 一句话定位 | 复用价值 |
|---|---|---|---|
| evoagent | 12.8k 行 Python | 自研 PR 审查引擎,路线正确但产品化完成度低 | 审查领域资产:规则、finding 模型、多 Agent 编排语义、记忆/技能进化设计 |
| pr-agent-0.43.0 | 47.8k 行 Python(MIT) | 成熟 PR Agent,但无 Harness/记忆/评测 | **输入入口层复用 ~90%**:命令分发、webhook 模式、git_providers 抽象、并发模型 |
| AutoCVE | 84.7k 行后端 + 46k 行前端(AGPL-3.0) | 代码审计平台,完成度最高 | **通用运行时底座复用 ~70%**:QueryLoop、工具层、LLM 层、事件/持久化、全产品栈 |
| deepseek-harness | 190+ TS 包 | 最强 Agent Harness,全 TS + Cordis fork(rc.8) | 只借鉴设计:事件溯源 session、工具管线、snapshot 重放评测 |

**核心决策**:以 **AutoCVE 为底座**改造,**先做冗余清理(删除 legacy 引擎与漏洞专用代码)**,再移植 **evoagent 的审查领域资产**,借鉴 **pr-agent 的输入入口模式**,评测直接接 **code-review-benchmark**。deepseek-harness 不进主线。

## 2. 为什么以 AutoCVE 为底座

1. **运行时完成度最高**:QueryLoop(1700 行 ReAct 运行时,含 nudge/压缩/checkpoint/恢复)vs evoagent runtime(219 行)——不在一个量级,这正是 PRD 角度 3(Runtime Harness)的核心要求。
2. **领域耦合最薄**:通用层(runtime_core + QueryLoop + SessionStore + LLM 层 + 权限/hook/checkpoint)占后端一半以上,且**零漏洞语义**;领域语义集中在薄薄一层"皮"(prompt 文案 + 终结 payload schema + 结果表),是替换不是重构。
3. **产品栈完整**:前端(Agent 状态树/活动日志/SSE 流式)、任务队列、alembic 迁移、103 个测试,改造完直接是产品而非引擎。
4. **许可已确认**:AGPL-3.0 对开源产品(本项目定位)无限制。
5. **冗余可控**:源码实测确认 AutoCVE 处于 legacy/runtime 双引擎过渡态,legacy 引擎(analysis_workflow 系 ~1.2 万行)在编排主流程中 90% 不被调用,可整组删除,不会伤及运行时。

## 3. 目标架构

```
┌─ 输入入口层(阶段 1,借鉴 pr-agent 模式)──────────────────────┐
│  FastAPI webhook 服务器(BackgroundTasks 并发,直接抄模式)     │
│  + 命令分发(pr-agent command2class 思路,130 行零依赖)        │
│  + PR importer(新写:clone PR 分支落盘,~100 行)              │
│  + plain-diff 输入(CLI/stdin,benchmark 离线评测入口)         │
│  + ★ 上下文收集器(新写):diff 之外上下文                     │
│      git 历史(提交信息/作者/变更意图)                       │
│      相关文件(diff 引用分析:imports/调用/定义/测试)         │
│      CI 状态(可选:构建/测试结果回读)                        │
│  + git_providers 抽象(初期:GitHub + plain-diff 两个实现)     │
└──────────────────────────────────────────────────────────────┘
┌─ 审查运行时(阶段 2,AutoCVE 底座)───────────────────────────┐
│  保留:QueryLoop / runtime_core 工具层 / LLM 层(12 provider   │
│       热切换)/ 权限 / hook / checkpoint / SessionStore       │
│  删除:legacy 引擎(analysis_workflow/finding_*/旧工具集)      │
│  Agent 体系(本架构核心,三专业 Agent):                       │
│    Orchestrator:接收 PR payload → 收集上下文 → 分发专业 Agent │
│                  → 综合结果(去重/优先级/格式化输出)           │
│    Security Agent:OWASP 视角;依赖扫描 + SAST 结果反馈 LLM     │
│    Architecture Agent:模块边界 / 跨文件依赖 / 分层违规         │
│    Code Quality Agent:样式一致性 / 测试覆盖 / 边界情况        │
│  新增:finalize_review 终结工具 + review 评论 schema           │
│  综合层(第三层):merge 去重(file+line+category)→ 严重度分配    │
│                → 格式化为 PR 评论(限条数,低噪)                │
└──────────────────────────────────────────────────────────────┘
┌─ 领域资产(阶段 2 移植,evoagent 来源)───────────────────────┐
│  finding 模型(rule_id/severity/path/line/evidence)           │
│  审查规则(本地确定性规则,补 AutoCVE 无规则兜底的短板)        │
│  记忆系统 + Skill 进化设计(PR 语义化)                        │
└──────────────────────────────────────────────────────────────┘
┌─ 评测闭环(阶段 3,code-review-benchmark)───────────────────┐
│  离线 50 PR(5 仓库 × 10 PR,human-verified golden)           │
│  注入点:benchmark_data.json reviews[].review_comments        │
│  LLM judge 打分:precision / recall / F-beta + 门禁           │
└──────────────────────────────────────────────────────────────┘
产品壳(阶段 4):AutoCVE React 前端保留,页面语义替换
```

### 3.1 Agent 设计(与行业多 Agent 审查架构对齐)

```
┌─────────────────────────────────────────────────────┐
│             Orchestrator Agent                      │
│  (Coordinates review, deduplicates,                 │
│   prioritizes, formats output)                      │
└───────────┬──────────────────┬──────────────────────┘
            │                  │
   ┌────────▼───┐     ┌────────▼───────┐     ┌────────▼──────────┐
   │ Security   │     │ Architecture   │     │ Code Quality      │
   │ Agent      │     │ Agent          │     │ Agent             │
   │ (OWASP     │     │ (coupling,     │     │ (style, tests,    │
   │  scan)     │     │  layering)     │     │  coverage)        │
   └────────────┘     └────────────────┘     └───────────────────┘
```

- **Orchestrator(第一层)**:接收 PR payload,收集上下文(git 历史、相关文件、CI 状态),将任务分发给三个专业 Agent,综合结果。编排器最关键的功能是**上下文组装**——在 Agent 开始工作之前收集其所需的一切信息。
- **专业 Agent(第二层)**:每个 Agent 有特定 prompt 和工具集。Security 调用依赖扫描器 + SAST,结果反馈到 LLM 审查流程;Architecture 读取模块边界和跨文件依赖;Quality 按项目样式指南检查特定模式。**协作方式:调度平级、通信星型**——各自独立 session 执行,仅经 TaskHandoff 回传结构化结果,不做平级互聊;必要时 Orchestrator 受控追问(协议见 `02-runtime.md` §3.2)。
- **综合与优先级排序(第三层)**:合并重叠结果(同一行、同一问题),分配总体严重程度,格式化为 PR 评论(初始只报 critical/high,控制噪声,逐步扩展)。

## 4. 数据流

```
PR 事件/CLI/diff ──▶ 输入入口(webhook/CLI/plain-diff)
      │  校验(HMAC/幂等)
      ▼
PR importer ──▶ clone PR 分支到持久化目录 + 提取 diff
      ▼
上下文收集器 ──▶ git 历史 + 相关文件(diff 引用分析) + CI 状态
      ▼
Orchestrator ──▶ 分发 Security / Architecture / Code Quality 三 Agent(并行)
      │           (每个 Agent:QueryLoop ReAct + 工具 Read/Glob/Grep/Bash + SAST 反馈)
      ▼
综合层(第三层) ──▶ merge 去重(file+line+category) ──▶ 严重度分配 ──▶ 评论格式化(限条数)
      ▼
finalize_review(结构化终结) ──▶ 评论落库(JSON)
      ▼
前端展示 / VCS 评论回写 / benchmark 评测
```

## 5. 阶段划分与交付

| 阶段 | 文档 | 内容 | 预估 |
|---|---|---|---|
| 00 | 本文 + 冗余分析 | 架构定稿、**Spike 验证核心假设**、**分层移植**（移植=复制+去冗余+重命名+文案替换） | 5-7 天 |
| 01 | `01-input.md` | PR 输入入口(webhook + CLI + importer + plain-diff)+ **上下文收集器** | 4-6 天 |
| 02 | `02-runtime.md` | 审查运行时 PR 化(Orchestrator + 三专业 Agent + 综合层 + schema + prompt + 领域资产移植) | 6-9 天 |
| 03 | `03-evaluation.md` | 评测闭环(benchmark 离线接入 + 门禁) | 2-3 天 |
| 04 | `04-product.md` | 前端保留改造 + 发布 | 后续 |

主线(01-03)完成后达到:**PR 进来 → 上下文收集 → 三 Agent 并行审查 → 综合 → 结构化评论 → benchmark 离线打分** 的全闭环。

### 5.1 阶段 00 实施路线（Spike + 分层移植）

**实施决策**:采用**方案一(先移植后开发)**,不做"在 AutoCVE 内改完再搬"的方案二。理由:① 避免同一批文件动两遍(删 legacy + 改文案在搬移时一次完成);② AutoCVE 的 git 历史仅 3 个 upload commit,保留价值为零,新仓库历史即 CodeSage 自己的;③ 按依赖层移植,每层单测可独立验证,import 断裂可定位到层;④ 开发期工作区即目标形态,无死代码噪音(grep 领域词、IDE 跳转、测试收集均不受干扰)。AutoCVE 子目录**保持只读**作上游参照。

**Phase 0 — Spike(在 AutoCVE 内,2-3 天,实验分支,不提交正式代码)**:✅ **已完成(2026-08-31)**,交付物 `AutoCVE/backend/spike_pr_review.py`(单文件端到端:会话创建→工具注册→ReAct 循环→FinalizeReview 终结→零改表落库;**真实 DeepSeek 实测跑通**,`SPIKE_MOCK_LLM=1` 可无网络运行)。

| 验证项 | 结果 | 证据/结论 |
|---|---|---|
| ① 终点工具参数化 | ✅ 可用 | 终结判定是 **payload 驱动**的(query_loop.py:545-561):工具返回 `output_payload['final_payload']` 即触发 COMPLETED+FINALIZE_TOOL,自定义 `FinalizeReview` **无需改运行时**即可终结(实测 stop_reason=completed / completion_mode=finalize_tool) |
| ①b 硬编码点 | ⚠️ 2 处需参数化 | query_loop.py:950-951 `has_terminal_tool_call` 集合 `{FinalizeFinding, FinalizeVulnerabilityReports}`;query_loop.py:1528-1534 按工具名推断 terminal_action 的回退分支。建议给 QueryLoop 增加 `finalizer_tool_names` 构造参数(仅影响 nudge 触发判断,不影响终结) |
| ①c 工具 schema 转换 | ⚠️ 移植必带 | ModelClient 必须做 `_to_llm_tool_schema`(bridge.py:338-347):`{name, description, input_schema}` → `{type:"function", function:{...}}`。spike 曾因直接透传导致 LiteLLM 丢弃全部工具(`dropping unsupported tool type(s) ['None']`),模型退化为伪工具语法 |
| ② PR 语义 runtime 跑通 | ✅ 真实 LLM 实测 | DeepSeek(deepseek-chat)真实运行:模型原生调用 Read/Glob/Bash(工具真实执行,失败后优雅降级)→ 基于 diff 自证发现 SQL 注入(自算行号 14)→ FinalizeReview 提交 3 条结构化评论(critical/medium/low)→ 正常终结 |
| ③ 工具裁剪 | ✅ | `registry.register(FinalizeReviewTool())` 手动注入即可;模型可见 12 个工具(无 FinalizeFinding)。生产在 runtime_tool_registry.py:586 加 review 分支 |
| ④ 零改表落库 | ✅ | 消息 42/回合 6/工具调用 10/checkpoint 21 行全部落 audit_session 系列表(SQLite 实测);SQLAlchemy JSON 列兼容,无 PostgreSQL 专有类型 |

**移植保留清单(Spike 产出)**:原样搬 `session_store`/`query_loop`(除 2 处硬编码)/`runner`/`bridge`/`tool_runtime`/`llm`/`tool_message_codec`;小改后搬 `query_loop.py`(finalizer_tool_names 参数化)+ `runtime_tool_registry.py`(review 分支)+ model client(`_to_llm_tool_schema` 转换);新写 `review_runtime/tools/finalize_review.py`(spike 中 FinalizeReviewTool 的生产版)。另:模型客户端可用 SpikeModelClient(bridge.py:69 RuntimeLLMModelClient 的最小等价版)对照理解。

**Phase 1 — 分层移植(根仓库 `backend/`,每层一个 commit + 测试绿)**:

| 层 | 内容 | 验证方式 |
|---|---|---|
| ✅ L0 基础设施(`b6cba56`) | pyproject(改名 codesage-backend)→ core/config → db/base+session → models(精简 7 文件/23 表);alembic 后置到 L4 | ✅ import + metadata 23 表 |
| ✅ L1 LLM 层(`555cc0d`) | services/llm(12 provider,原样)+ agent/core 零依赖件(errors/retry)+ json_parser | ✅ LLMService 实例化 + protocols 导入 |
| ✅ L2 运行时(`15ab351`+验收`c3957db`) | runtime_core(删 runtime_session_checkpoint_store)→ **finding_runtime 以 `review_runtime` 落位**(33 文件字节级 import 重写;排除 resume_job/resume_queue)→ agent_runtime → scan_runtime → triage_runtime;连带 skills_runtime/skill_service/工具最小集+shared_catalog 5 文件。tooling/memory re-export 暂保留 | ✅ 12/12 导入 + 移植版 spike mock+真实 DeepSeek 双模式跑通 |
| ✅ L3 编排(`baab37f`) | **orchestrator.py 即 runtime 版编排器(实测零 legacy 引擎引用)**/base.py(含 TaskHandoff 契约)/recon.py(context_collector 改造基础)/schemas/event_manager/event_stream/task_queue/task_executor/config/core(message/registry/state)/prompts/sandbox_tool(懒加载依赖) | ✅ compileall 全库 + 14/14 导入 + OrchestratorAgent/TaskHandoff 符号 |
| ✅ L4 API/worker(`054c7ae`) | 12 端点精简路由(删 8 个 legacy 端点);手术剥离 audit_sessions CVE 域(1152→951,追问改走 FindingRuntimeBridge)/projects legacy 扫描/scanner legacy 引擎(598→322,保留 repo 文件访问工具);init_db 精简版;agent_worker+streaming+git_ssh_service 等服务闭包;skill_library(121 文件)资产;alembic 23 迁移(未裁剪,升级时建 legacy 空表无害) | ✅ compileall 全库 + FastAPI 装配 114 路由 + uvicorn 启动 /health=200 |
| ✅ L5 测试基线(`e40546f`) | 7 个测试目录(105 用例);剔除 legacy/CVE 域与 API 集成测试(test_agent_contracts/test_agents/test_finding_v2/test_tools/test_compose_workspace_config/one_click_cve 系/checkmarx 系);修复 pyproject BOM、tools/__init__ 再导出、补 graph_controller+finding_skill_protocol | ✅ **pytest 104 passed / 1 skipped / 0 failed**(155s) |
| L6 前端(可后置) | frontend 搬骨架 + 删 OneClickCVE/Checkmarx/InstantAnalysis 三页 + 文案替换 | tsc + 构建(阶段 04 再深化) |
| ✅ 01 输入层(`fafd116`,PR #3) | pr_review 服务包 11 文件: 双输入模式(diff-only CLI / diff+上下文 webhook)+ context_collector 四维度(git 历史/相关文件确定性分析[import>caller>test+字节预算]/CI/用户注入)+ ReviewContext 契约 + webhook(HMAC+幂等+并发上限)+ CLI benchmark 通道; pr-agent 思路移植(command2class/BackgroundTasks/DefaultDictWithTimeout 内存版); 零新依赖; 模块说明 docs/modules/01-input.md | ✅ tests/pr_review 30 用例; 全量回归 **134 passed / 0 failed**; 装配 117 路由; CLI E2E(diff→context 落盘→JSON 评论) |
| ✅ 02 运行时语义(`c06fb3e`,PR #4) | FinalizeReview 终结工具+ReviewFinding 契约(source 归因, benchmark 类别对齐); 三视角星型编排(独立 session 黑盒并行+权限矩阵+TaskHandoff); 综合层(去重/严重度合并/低噪 critical/high/落行校验); 受控追问 ≤2 轮; 确定性规则引擎(evoagent 6 条+5 扩充); engine=rules 离线默认 / runtime LLM 编排; 底座参数化(注册表/query_loop 终点名/bridge agent_type+文案); 模块说明 docs/modules/02-runtime.md | ✅ 全量回归 **178 passed / 0 failed**; fake_runtime 驱动真实 QueryLoop 实测终结+nudge; spec §6 全覆盖 |
| ✅ 03 评测闭环 | `codesage_eval` 固定 fixture → 正式 AgentTask/双 Worker → 独立 judge 一对一匹配 → JSONL/离线 HTML；Phoenix 只做 Trace/experiment，评测不写产品 ORM；旧 step1.5/step3.5/eval_gate 旁路已删除 | 工程自动验收通过；真实模型质量基线与人工校准须显式提供端点、预算后执行 |
| ✅ 04 产品壳(前端保留改造) | AutoCVE React 前端整体移植为仓内 frontend/; 删 OneClickCVE/CheckmarxScan/InstantAnalysis 三页(路由+侧边栏+专属 API+features/checkmarx|analysis); 品牌 AutoCVE→CodeSage 全局清零(package.json/index.html/localStorage 键/Agent 闪屏 ASCII 横幅); 新增 shared/types/review.ts + api/prReviews.ts(ReviewComment/ReviewFinding 契约+engine=rules\|runtime+filterByPerspective 视角筛选); AgentAudit/Sessions/Skills/Admin 通用复用零改动; 模块说明 docs/modules/04-product.md | ✅ tsc 0 错误; vite build 通过; node:test 11/11; 品牌残留 0; diff 视图/组件测试(vitest 需 Ask-first)列入增强 |

**移植纪律**:

1. **每层一个 commit + 测试绿**,禁止跨层大提交;每删一个文件先 grep 引用(冗余分析已给出引用证据);
2. **license 合规**:根目录保留 AGPL-3.0 LICENSE;移植文件头注明来源(AutoCVE AGPL / pr-agent MIT);evoagent 资产自研无需声明;
3. **命名空间隔离**:backend 独立 pyproject 与测试树,`backend.app.*` 不与根仓库既有 `codesage.*` 包冲突;
4. **文案替换随层进行**:移植到哪一层就替换哪一层的 `审计|漏洞|CVE|FinalizeFinding|vulnerability` 命中(不预留"搬完再统一改"的批次);
5. **上游参照**:AutoCVE 子目录只读,任何"这个文件改了什么"以 git diff 对比 AutoCVE 为准。

**Phase 1 完成后**:根仓库 `backend/` 即目标形态(通用运行时零领域语义、无 legacy、review_runtime 命名),直接进入阶段 01-03 的 PR 特有开发。

## 6. 全局不变量

1. **通用运行时零领域语义**:runtime_core/QueryLoop/SessionStore/LLM 层禁止出现"审计/漏洞/CVE"或"PR/审查"字样;领域只存在于 Agent 定义、prompt、终结工具、schema 四层"皮"上。
2. **生成-审查隔离**:审查 Agent 只看 diff + 相关代码,不依赖"生成方的推理"。
3. **结构化终结**:所有 Agent 必须以终结工具提交结果,自然语言"我完成了"不算完成(nudge 上限 2,超限标记 incomplete)。
4. **评测优先**:任何能力(规则、prompt、记忆、技能进化)上线前必须在 benchmark 上对比回归,不进"自说自话"。
5. **master 只收合并**;每阶段 `feat/0N-xxx` 分支,代码 + 本规格 + 测试全绿后合并。
6. **上下文先于分发**:Orchestrator 必须在分发前完成上下文组装(这是本架构与"单 prompt 审 diff"的根本区别)。
7. **低噪优先**:初始只报 critical/high 评论,按 benchmark precision 逐步放宽(参考 2026 行业实践:每 PR 5-10 条)。

## 7. 边界与风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| legacy 引擎删除影响面(agent_tasks.py 3303 行耦合) | 中 | 分层移植时删;每删一个文件跑对应测试 + `pytest --collect-only` 全量 import 扫描 |
| 移植期间无完整产品可跑 | 中 | L0-L2 每层单测独立验证(纯单元,不依赖 Docker/Redis);L4 起才需 DB;前端后置不阻塞主线 |
| 终点工具参数化假设未实测 | 中 | **Phase 0 Spike 先行验证**,拿到移植保留清单后再分层移植 |
| QueryLoop 中文 nudge/恢复文案替换 | 中 | 文案替换随层进行,机械替换,用单测锁定文案不变量 |
| benchmark 静态数据集泄露(模型训练见过) | 低 | 已知局限,配合在线数据集/自建样本 |
| LLM judge 方差 | 低 | 多 judge model 并报(现有 3 个已配置) |
| AGPL 传染 | 无 | 已确认开源定位;根 LICENSE + 文件头声明 |
| pr-agent 入口层是 MIT,需保留版权声明 | 低 | 移植文件头注明来源 |
| 三 Agent 并行成本 | 中 | 每视角独立 session 并行;token 预算按视角分配,Orchestrator 汇总控制 |
