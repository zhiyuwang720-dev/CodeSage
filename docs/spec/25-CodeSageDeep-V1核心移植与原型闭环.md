# Spec 25：CodeSageDeep V1 核心移植与原型闭环

版本：v1.0  
状态：冻结  
Plan：`docs/plan/25-CodeSageDeep-V1核心移植与原型闭环.md` v3.1  
基线：CodeSage `a9b0f49e5d29bb835c5eae56f59df4ca6f34e21d`  
PR-AF：`48ae7eeb4f07779004db6354728d49ca7b36dbc3`  
OCR：`85cecfe5f935da2b2aae8f91ce4fee8ed343a681`

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

1. `agents/` 只依赖 `services/runtime.py` 的 Protocol 和 domain schema。
2. 只有 `services/runtime.py` 允许 import `app.execution_plane.runtime`。
3. `RuntimeBridgeDeepReviewRuntimeFactory` 按 role 构造/持有 RuntimeBridge，并注入四个 Deep Review tools。
4. 禁止 import quick-review、PR tool gateway、API、Worker、ORM、SQLAlchemy 或 GitHub client。
5. Prompt 全部位于 Markdown；Python 不保存长角色提示词。

## 4. Schema 冻结

### 4.1 输入

```python
class ReviewInput(BaseModel):
    repo_path: str
    base_ref: str
    head_ref: str
    title: str = ""
    description: str = ""
    commit_messages: list[str] = Field(default_factory=list)
```

只支持本地 Git worktree。输入层固定 merge-base/base 与 head 的 diff 和 commit messages，不 checkout，不访问网络，不改工作区。

### 4.2 配置

`DeepReviewConfig` 必须包含：

| 字段 | 默认值 | 语义 |
|---|---:|---|
| `max_duration_seconds` | `1800` | run 硬时长 |
| `max_concurrent_reviewers` | `4` | 全局 Reviewer semaphore |
| `max_planner_dimensions` | `8` | Planner 目标上限，不是 Repair 后硬上限 |
| `max_final_dimensions` | `9` | 正常 Repair 后硬上限 |
| `max_files_per_work_item` | `12` | 单 dimension 目标文件上限 |
| `max_cross_context_bytes` | `None` | V1 不设 Cross 上下文字节上限 |
| `max_candidate_count` | `None` | V1 不按数量抽样 |
| `max_evidence_bytes_per_finding` | `24000` | 单 Candidate 证据上限 |

`max_final_findings` 默认 `None`；只能在最终排序后截取，不能反向影响 Planner、Reviewer 或 Cross。

### 4.3 ReviewDimension

```python
class ReviewDimension(BaseModel):
    name: str
    review_prompt: str
    target_files: list[str]
    context_files: list[str] = Field(default_factory=list)
    priority: int = 1
    fallback: bool = False
    deferred: bool = False
```

dimension 是调查职责，不是 Finding 配额。Reviewer 可以返回零到多个 Finding。

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

## 7. Planner 与 Plan Repair

Planner 使用一次 `.harness(schema=ReviewPlan)`。整体解析、schema 或 harness 失败时 run failed；不猜测模型意图。

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

## 8. Reviewer

每个 repaired dimension 运行一次 `.harness(schema=ReviewerResult)`。

1. 所有 dimension 共享一个全局 `asyncio.Semaphore`。
2. 单个失败保留其他结果，run `partial`。
3. 全部失败 run `failed`。
4. 不创建 sub-review。
5. Reviewer prompt 合并 worthiness 规则，但没有 Finding 配额。

## 9. Candidate 与 Evidence

Candidate 稳定排序键：

```text
dimension_order, file_path, line_start, severity_rank, title
```

Evidence 由确定性代码提取，包含 primary code、diff hunk、caller snippets、cross-reference snippets、import context 和 related code。每个片段必须截断。单 finding 证据失败时留空并记录诊断，不删除 Candidate。

## 10. Cross Analysis

Cross 使用一次 `.harness(schema=CrossAnalysisResult)`，职责固定为：

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

## 11. 确定性收尾

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

## 12. 状态语义

| 状态 | 条件 |
|---|---|
| `completed` | 输入/filter 成功；semantic 或 planner 可 deterministic fallback；review/cross/evidence/收尾成功 |
| `partial` | 单 Reviewer 失败、Cross fallback、Plan Repair deferred、其他显式增强降级 |
| `failed` | 输入/filter/Planner 整体失败、全部 Reviewer 失败、确定性收尾失败 |

`asyncio.CancelledError` 不吞；尽力写 `run_cancelled` 后继续抛出。

## 13. JSONL 存储

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

## 14. CLI 与 AACR

CLI：

```powershell
python -m app.domains.deep_review --repo <repo> --base <base> --head <head>
```

返回码：

| 结果 | exit |
|---|---:|
| completed / partial | 0 |
| run failed | 1 |
| input/config error | 2 |

`--output` 写标准 `DeepReviewResult` JSON；未提供时写 stdout。日志写 stderr。

AACR adapter 只做子进程调用和 result envelope 转换，不修改 evaluator/judge/dataset。

## 15. 测试矩阵

| ID | 范围 | 必须验证 |
|---|---|---|
| D25-01 | Schema | JSON round-trip、必填、literal enum、metrics optional usage |
| D25-02 | Runtime usage | total/input/output 聚合；missing usage 不伪造成零 |
| D25-03 | Directory filter | secret/include/exclude/order/rename/deleted/binary |
| D25-04 | Tools | deleted unavailable、path escape、symlink、limits、Windows 无 Unix grep |
| D25-05 | Plan repair | invalid path、dedup、missing fallback、split、merge、deferred |
| D25-06 | Planner | schema finalization；整体失败 run failed |
| D25-07 | Reviewer | bounded concurrency、0..N findings、partial failure |
| D25-08 | Evidence | path gate、bounded snippets、failure keeps finding |
| D25-09 | Cross repair | missing/duplicate/out-of-range/empty drop/new finding validation |
| D25-10 | Finalization | deterministic score/dedup/polish，零模型调用 |
| D25-11 | JSONL | stable sequence、serial writes、no secrets、new run semantics |
| D25-12 | Orchestrator | completed/partial/failed/cancelled |
| D25-13 | CLI | stdout/stderr、exit code、offline fake runtime smoke |
| D25-14 | Static boundary | 禁止 quick-review/API/Worker/ORM/RuntimeBridge import 泄漏到 agents |

## 16. 验收命令

```bash
pytest -q backend/tests/deep_review
pytest -q backend/tests/runtime/test_bridge_deep_runtime.py
pytest -q backend/tests/runtime
```

## 17. 明确不做

1. Stage resume / harness resume。
2. Multi-process JSONL writer。
3. SQL storage 和 migration。
4. GitHub posting。
5. HITL。
6. 逐 finding merge/polish 模型调用。
7. Coverage loop、独立 Adversary、Consistency、Post-worthiness。
8. Worker/API/前端接入。
