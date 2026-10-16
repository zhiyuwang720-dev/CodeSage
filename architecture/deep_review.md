
# Deep Review：Stage Inputs & Outputs / 各阶段输入输出

Current implementation, with short field descriptions rather than complete JSON schemas.

以下按当前实现说明各阶段输入输出，字段仅作简要描述，不是完整 JSON Schema。

```text
Input & Filter → Blast Radius → Anatomy → Semantic → Planner & Repair
    → Parallel Reviewers → Evidence Extraction → Cross Analysis → Final
```

## 1. Input & Filter / 输入与文件筛选

Deterministic code resolves revisions, parses the diff in memory, and classifies changed files.

确定性代码固定 Git 版本，将 diff 加载到内存，并对变更文件分类。

Input — `ReviewInput`:

```text
repo_path: Local Git repository. / 本地 Git 仓库路径。
base_ref: Base revision for comparison. / 对比的基准版本。
head_ref: Revision being reviewed. / 待审查的目标版本。
title: PR title. / PR 标题。
description: PR description. / PR 描述。
commit_messages: Supplied commit messages. / 输入提供的提交说明。
```

Output — `ReviewSnapshot`:

```text
base_commit, head_commit, merge_base: Resolved commit hashes. / 固定后的提交哈希及共同祖先。
commit_messages: Commit messages available for review. / 本次审查可用的提交说明。
changes: Changed paths, types, patches, and line counts. / 变更路径、类型、补丁及增删行数。
diff_by_path: In-memory patches indexed by path. / 按文件路径组织的内存 diff。
review_paths: Files requiring review. / 需要审查的文件。
context_paths: Files retained as supporting context. / 仅作为辅助上下文保留的文件。
filter_result: Classification decisions and reasons. / 文件分类结果及原因。
```

## 2. Blast Radius / 关联范围分析

Deterministic code finds potentially related files. These are investigation leads, not confirmed impact.

确定性代码寻找可能相关的文件。这些是调查线索，不代表已经确认受到影响。

```text
Input:
    review_paths: Changed files requiring review. / 待审查的变更文件。
    repo_path, head_commit: Repository and fixed source revision. / 仓库及固定的源码版本。
    config: Search and result limits. / 搜索范围与数量限制。

Output:
    related_paths: Potentially related file paths. / 可能相关的文件路径。
    diagnostics: Search failures or degraded analysis. / 搜索失败或降级说明。
```

## 3. Anatomy / 变更结构分析

Deterministic code summarizes change structure without judging whether bugs exist.

确定性代码整理变更结构，不判断是否存在缺陷。

Input: review/context changes and related paths. / 输入：审查及上下文文件的变更、关联文件路径。

Output — `Anatomy`:

```text
files: File changes retained for analysis. / 参与分析的文件变更。
hunks: Parsed diff hunks grouped by path. / 按文件路径组织的差异块。
stats: File counts, changed lines, and test statistics. / 文件数、增删行数和测试相关统计。
clusters: Structural groups of changed files. / 变更文件的结构分组。
directories: Directories involved in the changes. / 变更涉及的目录。
related_paths: Files found by Blast Radius. / 关联范围分析找到的文件。
summary: Structural summary. / 变更结构摘要。
```

## 4. Semantic / 变更语义理解

One `.ai()` call interprets PR intent and visible changes. Its output provides investigation leads, not verified facts or Findings.

通过一次 `.ai()` 理解 PR 意图和实际变更。结果是调查线索，不是已验证事实或 Finding。

Input: PR metadata, commit messages, selected diff content, and Anatomy context. / 输入：PR 元信息、提交说明、选取的 diff 内容及结构分析上下文。

Model output — `SemanticBriefDraft`;          accepted output — `SemanticBrief`:

```text
narrative: Coherent description of what the PR does and where risk may arise. / 描述 PR 做了什么，以及风险可能出现在哪里。
stated_intent: Behavior promised by the title, description, or commits. / 标题、描述或提交说明声称要实现的行为。
implemented_intent: Behavior actually visible in the diff. / diff 中实际体现的行为。
intent_gaps: Promised behavior that appears absent or incomplete. / 声称要实现但看起来缺失或不完整的行为。
unrelated_changes: Changes not explained by the stated intent. / 无法由声明意图解释的变更。
risk_surfaces: Areas worth investigation, not bug conclusions. / 值得调查的风险面，而非缺陷结论。
hypotheses: Falsifiable questions for later review. / 后续需要验证或推翻的问题。
confidence: Confidence in semantic understanding. / 对语义理解的置信度。
source: Model or fallback, assigned by code. / 来自模型还是回退逻辑，由代码标记。
```

## 5. Planner & Repair / 调查规划与修复

One `.harness()` divides investigation work. Deterministic repair preserves questions, handles file overlap, and remaps relationships. Planning does not prescribe how many bugs must be found.

通过一次 `.harness()` 划分调查工作，再由确定性 repair 保全调查问题、处理文件重叠并重映射关系。Plan 不预设必须发现多少个问题。

Input: 
snapshot, 
Anatomy, 
Semantic leads, 
planning limits. 
Read-only tools
let the Planner check assumptions. / 输入：变更快照、结构分析、语义线索及规划限制。Planner 可用只读工具核对假设。

Model output — `ReviewPlanDraft`:

```text
summary: Overall investigation strategy. / 整体调查策略。
dimensions: Proposed cohesive review assignments. / 建议的内聚审查任务。
    name: Dimension name. / 维度名称。
    review_prompt: Questions and behavior to investigate. / 需要调查的问题与行为。
    target_files: Files to review together. / 本任务需要联合审查的文件。
    context_files: Supporting files, not additional targets. / 辅助理解的文件，不是额外审查目标。
    priority: Scheduling priority; smaller numbers run first. / 调度优先级，数字越小越优先。
    rationale: Why these questions and files belong together. / 这些问题与文件应一起调查的原因。
cross_reference_hints: Relationships to check across dimensions. / 后续需要跨维度检查的关系。
    dimension_names: Dimensions involved. / 涉及的维度名称。
    relation: Relationship requiring verification. / 需要验证的关系。
    symbol_or_contract: Relevant symbol or behavioral contract. / 相关符号或行为契约。
assumptions: Assumptions requiring verification. / 仍需核对的假设。
```

Accepted output — `ReviewPlan`, with repaired dimensions and additional fields:

接纳结果为 `ReviewPlan`，包含修复后的维度及以下字段：

```text
dimensions[].investigations: Original questions preserved in each final dimension. / 每个最终维度保留的原始调查问题。
    source_dimension: Original dimension name. / 原维度名称。
    review_prompt, priority, rationale: Original investigation instructions. / 原始调查指令、优先级和理由。
dimensions[].fallback: Whether this is a fallback assignment. / 是否为补齐遗漏文件的兜底任务。
dimensions[].deferred: Whether configured limits deferred execution. / 是否因配置限制暂缓执行。
dimensions[].source: Model, fallback, merged, or split. / 维度来自模型、兜底、合并还是拆分。
dimension_name_map: Original names mapped to final names. / 原维度名称到最终名称的映射。
coverage_complete: Whether the plan covers all required files. / 计划是否覆盖全部待审查文件，不代表审查已完成。
repair_actions: Deterministic changes made to the draft. / 对 Draft 所做的确定性修复记录。
unresolved_risks: Remaining planning limitations. / 规划后仍未解决的限制或风险。
```

## 6. Parallel Reviewers / 并行审查

Each active dimension runs a `.harness()`, bounded by a shared concurrency limit. Reviewers investigate and apply post-worthiness checks before returning candidates. Zero candidates is valid.

每个待执行维度运行一个 `.harness()`，受共享并发上限约束。Reviewer 调查问题，并在输出前执行内置的 worthiness 判断；允许没有候选问题。

Input: dimension instructions, target/context paths, target diffs, fixed revisions, and Semantic leads. Preserved investigations are rendered together in the prompt. / 输入：维度调查指令、目标及上下文路径、目标文件 diff、固定版本和语义线索。合并保留的调查问题一起传给 Reviewer。

Model output — `ReviewerResultDraft`; accepted output — `ReviewerResult`:

```text
summary: Summary of this dimension's investigation. / 本维度的调查摘要。
findings: Candidate issues awaiting Cross validation. / 待 Cross 验证的候选问题。
    file_path: File containing the reported issue. / 问题所在文件。
    line_start, line_end: Head-revision location, when available. / 可提供时标注目标版本中的行范围。
    severity: Critical, high, medium, or low. / 严重程度。
    title: Concise issue title. / 简洁的问题标题。
    body: Trigger conditions, faulty behavior, and impact. / 触发条件、错误行为及影响。
    evidence: Supporting code evidence. / 支撑问题的代码证据。
    suggestion: Optional correction advice. / 可选的修复建议。
    confidence: Confidence in the candidate. / 对候选问题的置信度。
    tags: Descriptive labels. / 问题标签。
    dimension_name: Originating dimension, assigned by code. / 所属维度，由代码补充。
    source: Set to reviewer by code. / 由代码标记为 reviewer。
```

The stage also returns per-dimension execution reports and an ordered `candidates` list. Later candidate indices refer to this list.

本阶段同时产出各维度执行报告和有序的 `candidates` 列表。后续候选索引均指向这份列表。

## 7. Evidence Extraction / 证据提取

Deterministic code collects bounded source excerpts; it does not decide whether claims are correct.

确定性代码提取有界的源码片段，不裁决候选问题是否成立。

Input: candidates, snapshot, related paths, and extraction limits. / 输入：候选列表、快照、关联路径及提取配置。

Output — candidate-indexed `EvidencePackage`:

```text
finding_index: Index in the ordered candidate list. / 有序候选列表中的索引。
primary_code: Source near the reported location. / 问题位置附近的源码。
caller_snippets: Related caller excerpts. / 相关调用方片段。
cross_ref_snippets: Cross-reference excerpts. / 交叉引用片段。
diff_hunk: Matching diff hunk, when available. / 可匹配时提取问题行所在的差异块。
import_context: Relevant import context. / 相关导入上下文。
related_code: Additional related source. / 其他关联源码。
truncated: Whether extracted evidence was truncated. / 提取的证据是否被截断。
```

## 8. Cross Analysis / 交叉验证与复合分析

One `.harness()` combines evidence verification, adversarial challenge, consistency checks, duplicate assessment, and cross-file compound-risk analysis.

通过一次 `.harness()` 完成证据验证、对抗挑战、一致性检查、重复关系判断及跨文件复合风险分析。

Actual model input / 实际发送给模型的输入：

```text
run_constraints: Fixed revisions, review paths, and turn limit. / 固定版本、审查范围和轮次限制。
candidate_claims: Every indexed claim, its evidence, and location status. / 全部候选的索引、问题内容、证据及位置匹配状态。
matched_hunks: Matching diff excerpts, deduplicated across candidates. / 与候选位置匹配并去重后的差异片段。
cross_hints: Relationships from the repaired plan. / 修复后计划中的跨维度关系线索。
coverage_gaps: Incomplete dimensions and files without successful review. / 未完成的维度及未成功审查的文件。
```

The prompt does not copy the full Semantic brief or every EvidencePackage field. Cross can inspect additional source using read-only tools.

提示词不会直接拼入完整 Semantic，也不会传入 EvidencePackage 的所有字段。Cross 可用只读工具继续核对源码。

EvidencePackage 到 Cross，保留和裁掉了什么？
需要区分两种“证据”：
- ReviewFinding.evidence：Reviewer 写出的证据描述，可能包含代码引用，它仍是待核验的主张。
- EvidencePackage：确定性代码提取的源码材料。
  实际传给 Cross 的内容如下：
  候选的路径、行号、严重程度、标题	全部保留
  候选的 body、evidence	全部保留，此处不再做摘要截断
  候选的 suggestion、confidence、tags、source	不传入
  EvidencePackage.diff_hunk	确认匹配候选行号后传入；相同文件、相同片段合并一次，并标注关联候选索引
  EvidencePackage.primary_code	不传入，即使已经提取
  caller_snippets、cross_ref_snippets、import_context、related_code	不传入；当前提取实现也没有填充这些字段
  完整 Semantic	不传入，避免再次把前序解释作为事实
  Plan 的跨维度关系线索	保留
  未完成维度、未成功审查文件	保留

Model output — `CrossAnalysisResultDraft`; repaired output — `CrossAnalysisResult`:

```text
decisions: Decisions on existing candidates. / 对已有候选的裁决。
    finding_index: Candidate index being judged. / 被裁决的候选索引。
    result: Keep or drop. / 保留或丢弃。
    reason: Evidence-based explanation. / 基于证据的理由。
    revised_severity: Optional severity correction. / 可选的严重程度修正。
new_findings: New cross-file or compound issues using ReviewFinding fields. / 新增跨文件或复合问题，沿用 ReviewFinding 字段。
unresolved_risks: Questions that remain unverified. / 仍无法验证的问题。
summary: Cross-analysis summary. / 交叉分析摘要。
```

Local repair checks indices and decision completeness. Missing decisions or Cross failure do not silently erase candidates; uncertainty is recorded.

本地 repair 检查索引与裁决完整性。缺失裁决或 Cross 失败时不会静默删除候选，而是记录未验证状态。

## 9. Final / 最终结果整理

Deterministic code applies Cross decisions, incorporates new findings, deduplicates, scores, and formats the final result. No additional LLM call is made.

确定性代码应用 Cross 裁决、纳入新增问题，再去重、评分并整理最终结果，不额外调用 LLM。

Input: candidates, Cross result, evidence, snapshot, stage outcomes, and configuration. / 输入：候选、Cross 结果、证据、快照、阶段执行情况及配置。

Output — `DeepReviewResult`:

```text
run_id: Identifier of this execution. / 本次运行标识。
status: Completed, partial, or failed. / 完成、部分完成或失败。
findings: Final retained issues. / 最终保留的问题。
summary: Final review summary. / 最终审查摘要。
unresolved_risks: Remaining uncertainty and coverage limits. / 剩余不确定性及覆盖限制。
metrics: File counts, dimensions, usage, cost availability, and duration. / 文件数、维度数、用量、费用可用性和耗时。
content_hash: Hash identifying final result content. / 最终结果内容的哈希。
candidate_count: Candidate count before Cross adjudication. / Cross 裁决前的候选数量。
cross_status: Completed, partial, or skipped Cross analysis. / Cross 完成、部分完成或跳过。
diagnostics: Execution and degradation notes. / 执行及降级诊断。
```

Stopping before Final returns a `PreparationReport` containing completed-stage outputs. A final run also produces a `ProcessReport` for stage results and observations; `DeepReviewResult` remains the authoritative final Findings output.

在 Final 之前停止时，返回包含已完成阶段结果的 `PreparationReport`。完整运行还生成汇总阶段结果与观测信息的 `ProcessReport`；最终 Findings 以 `DeepReviewResult` 为准。
