# Spec 25：CodeSageDeep V1 核心移植与原型闭环

版本：v2.0（分包执行索引与公共契约）  
状态：25.3A～25.9 已拆分为独立实施规格；文档交付不表示这些包已经实现  
Plan：`docs/plan/25-CodeSageDeep-V1核心移植与原型闭环.md` v3.4  
基线：CodeSage `a9b0f49e5d29bb835c5eae56f59df4ca6f34e21d`  
PR-AF：`48ae7eeb4f07779004db6354728d49ca7b36dbc3`  
OCR：`85cecfe5f935da2b2aae8f91ce4fee8ed343a681`

## 0. 如何使用分包 Spec

本文件保留整体契约摘要，具体开发以对应分包 Spec 的步骤、测试和交付清单执行。不要只阅读总索引就开始编写整个系统。每次执行一个包，先读本节、该包 Spec，再读其中列出的现有代码和 Plan 章节。

| 顺序 | 独立 Spec | 本包完成后的可观察结果 |
|---|---|---|
| 1 | [25.3A：渐进式编排与 Preparation 入口](25.3A-渐进式编排与Preparation入口.md) | CLI 离线运行到 anatomy，产出准备报告和 JSONL |
| 2 | [25.3B：Preparation 性能与 Blast Radius 边界](25.3B-Preparation性能与BlastRadius边界.md) | 批量读取 head blob，限制影响面扫描成本，稳定 Anatomy cluster |
| 2 | [25.4：Semantic、Planner 与 Plan Repair](25.4-Semantic与Planner及PlanRepair.md) | run 接入两个模型阶段，输出经过修复的调查计划 |
| 3 | [25.5：并行 Reviewer 与 Candidate 汇总](25.5-并行Reviewer与Candidate汇总.md) | 有界并行审查、部分失败记录和稳定 Candidate |
| 4 | [25.6：Cross Analysis 与确定性收尾](25.6-CrossAnalysis与确定性收尾.md) | 全部 Candidate 的裁决及可计算的最终结果 |
| 5 | [25.7：运行终态与 JSONL 可靠记录](25.7-运行终态与JSONL可靠记录.md) | 完整终态、用量、取消、写入失败和结果物化 |
| 6 | [25.8：CLI 契约冻结与 AACR 适配](25.8-CLI冻结与AACR适配.md) | AACR 可启动同一 CLI 并正确读取 comments |
| 7 | [25.9：真实 Smoke 与基准验收](25.9-真实Smoke与基准验收.md) | 一个真实 case 的工程证据和有边界的效果对照 |

### 0.1 实施者的工作顺序

1. 检查 git status，保留已有未提交实现，确认上一包确实通过验收。Plan 中的历史 HEAD 不是要求回滚的目标。
2. 阅读该包列出的实际接口。Spec 中的新函数是待实现设计，不能假定仓库里已经存在。
3. 按开发步骤实现最小可运行增量，同时接入 service.run 和 CLI。
4. 用表中的具体夹具验证业务边界，再运行 deep_review 回归；影响 Runtime 时补相关测试。
5. 核对交付标准，记录命令、结果、未通过项和下一包入口，按包分次提交。

各 Spec 中 services/、agents/、schemas/ 等生产路径均相对于 backend/app/domains/deep_review；tests/deep_review 相对于 backend。AACR 路径另行写出。文档中的建议测试文件若尚不存在，应在对应包创建。

### 0.2 对原 Plan 描述的实现澄清

以下事项已经同步到 Plan v3.4，避免不同实施者各自选择不兼容方案：

- Agent 内定义 Draft，并在该调用边界完成映射；repair/mapper 接收局部 dump 与可信输入，不反向 import Agent。跨阶段返回业务模型，禁止把 Draft 嵌入 schemas。
- PreparationReport 从 anatomy 起步，后续扩展 completed_stage 和可选业务摘要。只有 final 产生 DeepReviewResult；不能一直固定 anatomy 再把 planning 塞进去。
- max_final_dimensions 限制活动 Reviewer 数。deferred 组仍保留归属记录，但其文件未被审查，coverage_complete=false。
- Cross 必须处理所有 Candidate。取消示例中 decisions 的固定 256 条限制；容量失败明确 fallback，不抽样。
- 当前配置没有每 dimension 的 token 硬预算。文件数限制是已定义硬规则，token 估算只有接入实际容量参数后才能宣称生效。
- 同输入、同模型输出的确定性转换可以稳定；独立两次 LLM 调用不保证相同结果。不同时间的整份事件文件也不保证相同 hash。
- RuntimeTool 所需 app.contracts.tools/models 是已有工具协议依赖，允许 tools 使用。只有 runtime adapter 可导入执行 Runtime；不引入旧 PR 业务模型。
- 25.8 在 evaluation/pipeline.py 做必要 reviewer 注册，并转换为现有 review.comments 格式。评测算法和 judge 保持原口径。
- 模型阶段 CLI 使用显式 --allow-model-calls；25.9 仍单 case 执行，judge 单独选择。

### 0.3 测试如何证明结果

fake runtime 用于证明编排、Schema、工具参数、失败处理与映射正确，不能证明模型真的会找到缺陷。真实效果只在 25.9 的固定 case 报告中讨论。

普通 CI 不调用真实模型、不连接生产数据库，也不依赖本仓库历史 commit。建议从 backend 执行 `$env:PYTHONPATH = "."`，然后 `pytest -q tests/deep_review`。测试文件须按当前包逐步新增，不能把尚未实现的包列为已通过。

## 1. 目标

实现独立领域 `backend/app/domains/deep_review/`，完成固定调用拓扑：

```text
1 Semantic .ai()
+ 1 Planner .harness()
+ W Reviewer .harness()
+ 1 Cross Analysis .harness()
+ 确定性 repair / evidence / scoring / merge / polish
```

不接入 quick-review、Worker、API、数据库业务表、GitHub 发布或前端。唯一共享运行能力是 RuntimeBridge `.ai()`、`.harness()` 和动态 `FinalizeReview`。

## 2. Runtime 前置契约

Plan25 前置改造已完成：

- `QueryLoopState` 聚合 `provider_tokens_used`、`provider_input_tokens_used`、`provider_output_tokens_used`、`provider_cost_usd`。
- `RuntimeModelResponse` 透传 `response_cost_usd`。
- `RuntimeBridge.harness()` 返回：
  - `usage.total_tokens`
  - `usage.input_tokens`
  - `usage.output_tokens`
  - `usage.usage_complete`
  - `usage.cost_available`
  - `usage.cost_usd`
- 当前 streaming model boundary 不返回 per-call cost，因此 `cost_usd=None`、`cost_available=False`。系统不得估算、继承非流式价格或伪造成本。

## 3. 领域边界

```text
backend/app/domains/deep_review/
├── agents/
├── prompts/
├── schemas/
├── services/
├── tools/
├── storage/
└── resources/
```

规则：

1. `agents/` 依赖 Runtime Protocol、Prompt loader、对应纯 mapper/repair 和业务模型；Agent 私有 Draft 不从包入口导出，services 不反向 import Agent Draft。
2. `schemas/` 禁止 import `agents/`，业务模型不得嵌套或引用 Agent Draft。
3. 只有 `services/runtime.py` 允许 import `app.execution_plane.runtime`。
4. `RuntimeBridgeDeepReviewRuntimeFactory` 按 role 构造/持有 RuntimeBridge，并注入四个 Deep Review tools。
5. 禁止 import quick-review、PR tool gateway、API、Worker、ORM、SQLAlchemy 或 GitHub client。
6. Prompt 全部位于 Markdown；Python 不保存长角色提示词。

## 4. Schema 冻结与模型/业务边界

### 4.1 三层数据边界

| 层 | 位置 | 生命周期 | 示例 |
|---|---|---|---|
| Agent Draft | `agents/<role>.py` | 只存在于一次 `.ai()`/`.harness()` 调用和紧随其后的转换 | `SemanticBriefDraft`、`ReviewPlanDraft` |
| 业务事实 | `schemas/` | 跨阶段传递、repair、最终输出 | `SemanticBrief`、`ReviewPlan`、`ReviewFinding` |
| 调用/阶段诊断 | `AgentCallResult`、run event | usage、session、cost、input fingerprint、fallback/error | `semantic_completed`、`plan_repaired` |

硬规则：

1. `schemas/` 不得 import `agents/`；业务模型不得包含 Draft 字段。
2. Draft 不从 `agents/__init__.py` 或 `schemas/__init__.py` 导出。
3. 模型不能填写 fallback/deferred/source/hash/session/usage/time 等业务事实。
4. Draft 到业务模型必须显式构造并重新校验；禁止用不校验的 `model_copy(update=...)` 注入业务字段。
5. 所有 Draft 使用 `ConfigDict(extra="forbid")`，每个字段有 `description` 和长度/范围/枚举约束。
6. Field description 会进入 Pydantic JSON Schema：`.ai()` 将其注入 system prompt，`.harness()` 将其作为动态 `FinalizeReview` 参数 Schema。
7. Draft 不直接成为下一阶段输入；下一阶段只读取业务模型、`ReviewSnapshot` 或确定性摘要。

### 4.2 输入与配置

`ReviewInput` 只支持本地 Git worktree 的 repo/base/head/title/description/commit messages。输入层固定 merge-base/base/head，不 checkout、不联网、不改工作区。

关键配置：

| 字段 | 默认值 | 语义 |
|---|---:|---|
| `max_duration_seconds` | `1800` | run 硬时长 |
| `max_concurrent_reviewers` | `4` | 全局 Reviewer semaphore |
| `max_planner_dimensions` | `8` | Planner 目标上限，不是 Repair 后硬上限 |
| `max_final_dimensions` | `9` | 活动 Reviewer 组硬上限；deferred 归属记录额外保留 |
| `max_files_per_work_item` | `12` | 单 dimension 文件上限 |
| `max_cross_context_bytes` | `None` | V1 不按字节抽样 Candidate |
| `max_candidate_count` | `None` | V1 不按数量抽样 |
| `max_evidence_bytes_per_finding` | `24000` | 单 Candidate 证据上限 |

`max_final_findings=None`；只有最终排序后可显式截取，不能影响 Planner、Reviewer 或 Cross。

### 4.3 Semantic Draft 与业务 SemanticBrief

`agents/semantic.py` 定义私有 `SemanticBriefDraft`。字段保持 `str/list[str]`，但必须具备：

- `narrative`：20..2000，PR 实际行为叙述，不是 Finding。
- 六个列表最多 20 项，单项 1..200 字符。
- `stated_intent` 仅来自 PR/commit 声称；`implemented_intent` 仅描述 diff 事实。
- `intent_gaps` 是声称但未体现；`unrelated_changes` 是实现但未声称。
- `risk_surfaces` 是调查面；`hypotheses` 是可证伪问题，不得写成缺陷结论。
- `confidence` 为 0..1 的整体语义理解置信度。

`.ai(schema=SemanticBriefDraft)` 成功后，Agent 像 PR-AF `_AnatomySemanticResult → AnatomyResult` 一样逐字段构造 `schemas.pipeline.SemanticBrief(source="model")`。禁止创建 `SemanticBriefEnvelope(brief=SemanticBriefDraft(...))`。

Schema/JSON/narrative 失败时直接构造 `SemanticBrief(source="fallback", confidence=0)`。base/head/diff hash/prompt version/session/usage/cost 写 stage event 或 `AgentCallResult`，不塞进 SemanticBrief；事件时间戳不参与稳定 hash。

### 4.4 Planner Draft 与业务 ReviewPlan

`agents/planner.py` 定义：

- `ReviewDimensionDraft`：name 1..64 且匹配 `[a-z0-9][a-z0-9_-]*`；review_prompt 20..2000；target_files 非空；priority 1..10（1 最高）。
- `CrossReferenceHintDraft`：引用 2..8 个 dimension name，并包含 relation 与 symbol/contract。
- `ReviewPlanDraft`：最多 32 dimensions、16 hints、10 assumptions。

`ReviewPlanDraft` 是 Planner 动态 `FinalizeReview` 的唯一 Schema。Agent 将已校验 Draft 的局部 dump 和 `ReviewSnapshot` 交给 `plan_repair.py`，显式构造 `ReviewPlan`；处理非法/重复路径、名称冲突、hint 引用修复、遗漏 fallback、拆分、合并和 deferred。业务 `ReviewDimension` 才包含 fallback/deferred/source，业务 `ReviewPlan` 才包含 coverage_complete/repair_actions。

### 4.5 Reviewer/Cross Draft

`agents/reviewer.py` 定义 `ReviewFindingDraft/ReviewerResultDraft`。模型不填 dimension_name/source；`reviewer_result_mapper.py` 校验 target path、head 行号和行区间后构造业务 `ReviewFinding(source="reviewer")`。

`agents/cross_analysis.py` 定义 `FindingDecisionDraft/CrossFindingDraft/CrossAnalysisResultDraft`。模型只填请求内 index、裁决和新复合风险；`cross_repair.py` 构造业务 `FindingDecision/CrossAnalysisResult/ReviewFinding(source="cross")`。

终结 Schema 映射：

| Agent | Runtime schema | 转换 |
|---|---|---|
| Semantic | `SemanticBriefDraft` | Agent 内直接构造 `SemanticBrief` |
| Planner | `ReviewPlanDraft` | `plan_repair.py → ReviewPlan` |
| Reviewer | `ReviewerResultDraft` | `reviewer_result_mapper.py → ReviewerResult` |
| Cross | `CrossAnalysisResultDraft` | `cross_repair.py → CrossAnalysisResult` |

## 5. Git 输入与 Directory Filter

Directory Filter 固定顺序：

```text
binary
→ secret path
→ user exclude
→ user include
→ supported extension
→ default exclude
→ deletion
→ size / token limit
```

规则：

1. secret 永远排除，include 不能重新纳入。
2. user include 可重新纳入普通 default exclude。
3. 删除文件为 `context_only`。
4. 删除文件只能通过 `file_read_diff` 读取；`file_read` 返回 `unavailable_deleted_file`，不算非法路径。
5. 路径统一 POSIX 相对路径。
6. 支持 `!`、anchored、directory-only、`*`、`**`、brace expansion。
7. 每个决定必须包含稳定 reason。

## 6. 只读工具

| 工具 | 来源 | 语义 |
|---|---|---|
| `file_read` | fixed head commit | 读取 head 快照；deleted path 返回 unavailable |
| `file_read_diff` | 内存 diff map | 只读 patch，不重复执行 Git |
| `file_find` | `git ls-tree` | 按路径关键字定位 |
| `code_search` | `git grep` | 字面量或正则搜索 |

约束：

1. 拒绝绝对路径、`..`、symlink escape 和 secret path。
2. 服务端强制行数、字节、结果数和 timeout 上限。
3. 不开放 shell、写文件、网络、数据库或 GitHub。
4. 工具实现为 RuntimeTool 协议，并可与动态 `FinalizeReview` 放入同一 ToolRegistry。

## 7. 渐进式编排与阶段数据流

### 7.1 Package 25.3A Preparation Pipeline

25.4 前先实现 `DeepReviewService.run()`、`services/orchestrator.py`、`__main__.py` 和最小 JsonL event store。当前只运行：

```text
ReviewInput
→ ReviewSnapshot（resolve/filter/diff/commit messages）
→ compute_blast_radius(review_paths, fixed head)
→ build_anatomy(changes, related_paths)
→ preparation report
```

当前 report 必须包含 `mode="preparation"`、`pipeline_complete=false`、`completed_stage="anatomy"`、固定 commits、review/context paths、excluded count、stats、clusters 和 related paths。它不是 `DeepReviewResult`，不得使用 completed/partial 假装审计完成。

该输出定义为 `schemas.output.PreparationReport`，不使用自由 `dict`。`mode`、`pipeline_complete` 和 `completed_stage` 分别是 `Literal["preparation"]`、`Literal[False]` 和 `Literal["anatomy"]`；其余字段是 run id、base/head commit、review/context paths、excluded count、`DiffStats`、`ChangeCluster` 和 related paths。

CLI stderr 输出每个阶段的 started/completed/failed 和 duration；stdout 只输出 JSON。Preparation 不构造 RuntimeBridge、不调用模型。

`DeepReviewService.run(review_input, *, through=...)` 在 `through="anatomy"` 时返回 `PreparationReport`，只在完整 `through="final"` 时返回 `DeepReviewResult`。当前 Package 不支持的 stage 必须在运行前拒绝，禁止静默降级。

### 7.2 内存状态

Orchestrator 使用内部 `DeepReviewRunContext` dataclass 持有 snapshot、anatomy、blast radius、semantic、plan、reviewer results、evidence、cross result 和 diagnostics。它不是 Schema、不会传模型、不会整体持久化，也不是 resume checkpoint。

### 7.3 阶段转换

| 阶段 | 输入组合 | 产出 |
|---|---|---|
| Input | ReviewInput + config + Git | ReviewSnapshot |
| Anatomy | snapshot changes/review paths/head | Anatomy + blast radius |
| Semantic | PR metadata + commit messages + compact Anatomy/diff summary | Draft → SemanticBrief |
| Planning | paths + filter summary + Anatomy + SemanticBrief + hints + snapshot-bound tools | Draft → repair → ReviewPlan |
| Reviewer | repaired dimension + relevant diff + SemanticBrief + tools | Draft → mapper → ReviewerResult |
| Candidate | all ReviewerResult, stable sort | indexed ReviewFinding list |
| Evidence | candidates + snapshot + blast radius | bounded EvidencePackage map |
| Cross | all candidate summaries + evidence + Plan hints + Anatomy | Draft → repair → CrossAnalysisResult |
| Finalize | candidates + Cross result + evidence | DeepReviewResult |

前一阶段 Draft 不能进入下一阶段。每个输入 builder 是独立确定性函数并有单元测试。

### 7.4 渐进式交付规则

25.4 接入 Semantic/Planner，25.5 接入 Reviewer，25.6 接入 Evidence/Cross/确定性收尾，25.7 完成 durable events 和终态。每个 Package 必须同时更新 run、CLI fake-runtime smoke 和 stage event；禁止只完成孤立模块后留到最后组合。开发期 `run()` 的显式返回联合是 `PreparationReport | DeepReviewResult`，不得为了单一返回类型伪造审计终态。

## 8. Planner 与 Plan Repair

Planner 使用一次 `.harness(schema=ReviewPlanDraft)`。整体解析、schema 或 harness 失败时 run failed；不猜测模型意图。

Planner 成功后，`plan_repair.py` 是最终事实源：

1. 只接受当前 review 集合中的规范化路径。
2. 同一 target path 第一次出现优先。
3. 遗漏文件合并为 `fallback=True` dimension。
4. 过大 dimension 按稳定文件顺序硬拆分。
5. 拆分后超过 `max_final_dimensions` 时确定性合并。
6. 合并排序键：

```text
(priority asc, fallback asc, min stable target index, dimension.name)
```

7. 从尾部尝试合并相邻两个。
8. `target_files` 顺序拼接去重；`context_files` 取并集、稳定排序去重。
9. 合并导致超过 `max_files_per_work_item` 时跳过该对。
10. 合并后不再重新拆分。
11. 全部不可合并时，尾部 dimension 标记 `deferred=True`，target files 写入 `unresolved_risks`，run 为 `partial`。

Repair 成功但发生 fallback、拆分或合并时记录 degraded；最终 run 可为 `completed`。

## 9. Reviewer

每个 repaired dimension 运行一次 `.harness(schema=ReviewerResultDraft)`。

1. 所有 dimension 共享一个全局 `asyncio.Semaphore`。
2. 单个失败保留其他结果，run `partial`。
3. 全部失败 run `failed`。
4. 不创建 sub-review。
5. Reviewer prompt 合并 worthiness 规则，但没有 Finding 配额。

## 10. Candidate 与 Evidence

Candidate 稳定排序键：

```text
dimension_order, file_path, line_start, severity_rank, title
```

Evidence 由确定性代码提取，包含 primary code、diff hunk、caller snippets、cross-reference snippets、import context 和 related code。每个片段必须截断。单 finding 证据失败时留空并记录诊断，不删除 Candidate。

## 11. Cross Analysis

Cross 使用一次 `.harness(schema=CrossAnalysisResultDraft)`，职责固定为：

1. Evidence verification。
2. Adversarial challenge。
3. Cross-file consistency。
4. Compound risk。

规则：

1. 每个 Candidate 必须有唯一 decision。
2. 非法 index 忽略；重复 index 第一次优先；遗漏 index 默认 keep 并记录诊断。
3. 空 drop reason 改为 keep 并记录诊断。
4. 非法 revised severity 保留原值。
5. 新 finding 必须引用 review 集合内合法路径；行号超出 head 文件范围则丢弃。
6. Cross 整体失败时 keep all、不新增 finding，run `partial`。
7. 零 Candidate 且没有 explicit cross hints 时允许立即返回空结果。

## 12. 确定性收尾

`scoring.py`：

1. 使用 severity、confidence、evidence completeness、diff proximity。
2. 不使用 AI provenance、adversary、coverage multiplier。
3. 评分只排序，不覆盖 Cross 的 explicit drop。

`merge_gate.py`：

1. 不调用模型。
2. 应用 Cross decisions、路径/行号校验、最低质量门槛和稳定 dedup。
3. dedup 选择 severity/confidence 更高者，不简单保留第一条。

`polish.py`：

1. 不调用模型。
2. 只规范化 whitespace、标题、行号、severity、Markdown 和 suggestion。
3. 不改变事实主张。

## 13. 状态语义

| 状态 | 条件 |
|---|---|
| `completed` | 输入/filter 成功；semantic 可 deterministic fallback；Planner/Reviewer/Cross/收尾按契约完成且无 partial 条件 |
| `partial` | 单 Reviewer 失败、Cross fallback、Plan Repair deferred、其他显式增强降级 |
| `failed` | 输入/filter/Planner 整体失败、全部 Reviewer 失败、确定性收尾失败 |

`asyncio.CancelledError` 不吞；尽力写 `run_cancelled` 后继续抛出。

## 14. JSONL 存储

Port：

```python
class DeepReviewStore(Protocol):
    def append(self, run_id: str, record_type: str, payload: dict[str, Any]) -> None: ...
```

默认路径：

```text
.codesage/deep-review/<run_id>/events.jsonl
.codesage/deep-review/<run_id>/result.json
```

约束：

1. append-only。
2. Reviewer 并行任务不直接写文件；orchestrator gather 后稳定串行写。
3. 不写完整 diff、源码、prompt、transcript、secret 或 API key。
4. 单行先完整序列化再 append 并 flush。
5. 写失败 fail-fast。
6. 每次 run 创建新 run id；不 resume。
7. 时间戳使用 UTC ISO-8601。

25.3A 先实现 run/input/anatomy 的最小事件；25.7 增加并发 Reviewer 稳定写入、usage、cancelled 和 final result。Preparation 到达显式停止点写 `preparation_completed`，不是 `final_result`。

## 15. CLI 与 AACR

CLI：

```powershell
python -m app.domains.deep_review `
  --repo <repo> `
  --base <base> `
  --head <head> `
  --through anatomy `
  --output preparation.json
```

25.3A 只允许 `--through anatomy`，省略时到当前最远已实现阶段。输出 `pipeline_complete=false` 的 preparation report。以后每个 Package 扩展合法 stage；25.8 才把默认值冻结为 `final` 并默认输出 `DeepReviewResult`。

返回码：

| 结果 | exit |
|---|---:|
| 到达显式 preparation stage | 0 |
| completed / partial | 0 |
| run failed | 1 |
| input/config error | 2 |

`--output` 写当前 stage 的 JSON；未提供时写 stdout。日志写 stderr。完整链路才写 `DeepReviewResult`。

本地手工 smoke：

```powershell
python -m app.domains.deep_review `
  --repo E:/Mac/CodeSage `
  --base a4595a82 `
  --head a9b0f49e `
  --through anatomy `
  --output .codesage/deep-review/manual-preparation.json
```

该 SHA 组合不用于普通 CI；自动测试创建自己的临时 Git repo。

AACR adapter 只做子进程调用和 result envelope 转换，不修改 evaluator/judge/dataset。

## 16. 测试矩阵

| ID | 范围 | 必须验证 |
|---|---|---|
| D25-01 | Schema boundary | Draft 私有、业务 schema 不 import Agent、Field description 进入 JSON Schema、显式 Draft→business mapping |
| D25-02 | Runtime usage | total/input/output 聚合；missing usage 不伪造成零 |
| D25-03 | Directory filter | secret/include/exclude/order/rename/deleted/binary |
| D25-04 | Tools | deleted unavailable、path escape、symlink、limits、Windows 无 Unix grep |
| D25-05 | Plan repair | invalid path、dedup、missing fallback、split、merge、deferred |
| D25-06 | Semantic/Planner | Semantic Draft→business/fallback；Planner Draft finalization→repair；整体 Planner 失败 run failed |
| D25-07 | Reviewer | bounded concurrency、0..N findings、partial failure |
| D25-08 | Evidence | path gate、bounded snippets、failure keeps finding |
| D25-09 | Cross repair | missing/duplicate/out-of-range/empty drop/new finding validation |
| D25-10 | Finalization | deterministic score/dedup/polish，零模型调用 |
| D25-11 | JSONL | stable sequence、serial writes、no secrets、new run semantics |
| D25-12 | Orchestrator | 25.3A preparation stage；后续 completed/partial/failed/cancelled 渐进接入 |
| D25-13 | CLI | preparation report 不冒充完成、stdout/stderr、exit code、临时 Git repo/fake runtime smoke |
| D25-14 | Static boundary | 禁止 quick-review/API/Worker/ORM/RuntimeBridge import 泄漏到 agents |

## 17. 验收命令

```powershell
Set-Location backend
$env:PYTHONPATH = "."
pytest -q tests/deep_review
pytest -q tests/runtime/test_bridge_deep_runtime.py
pytest -q tests/runtime
```

## 18. 明确不做

1. Stage resume / harness resume。
2. Multi-process JSONL writer。
3. SQL storage 和 migration。
4. GitHub posting。
5. HITL。
6. 逐 finding merge/polish 模型调用。
7. Coverage loop、独立 Adversary、Consistency、Post-worthiness。
8. Worker/API/前端接入。
