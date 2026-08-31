# 阶段 02 模块说明 — 审查运行时 PR 化（Orchestrator + 三专业 Agent + 综合层）

> 规格:`docs/spec/02-runtime.md`(本地) · 分支:`feat/02-runtime-semantics` · 前置:阶段 01(ReviewContext)

## 1. 交付清单

### 新增(领域皮)

| 文件 | 内容 |
|---|---|
| `review_runtime/final_review_contract.py` | `ReviewFinding` 契约(rule_id/severity/category/title/description/file_path/line/confidence/needs_verification/verdict/**source 视角标记**)+ `FinalReviewPayload`;路径逃逸/行号区间校验;类别与 benchmark 对齐(bug/security/concurrency/data/api/perf/test_gap/doc_defect) |
| `review_runtime/tools/finalize_review.py` | **FinalizeReview 终结工具**(替代 FinalizeFinding):同机制——校验失败 `finalization_rejected` + 错误反馈给模型;成功 `terminal_action="finalize_review"` 触发终止 |
| `agent/prompts/review_prompts.py` | Orchestrator + Security/Architecture/Quality 三视角 system prompt + 受控追问消息模板(只注入结构化事实) |
| `pr_review/diff_lines.py` | 统一 diff → 新增行解析(head 行号);供规则引擎与落行校验共用 |
| `pr_review/rules.py` | 确定性规则引擎: **evoagent 原始 6 条(MIT, 文件头注明)** + 5 条扩充(concurrency/data/api/perf/test_gap);只审新增行;模型不可用时可独立出审 |
| `pr_review/synthesizer.py` | 综合层(§3.3 五步):归一化→去重(file+line+category,同 key 严重度取最高+来源合并)→排序→**初始只出 critical/high**→落行校验(非新增行拒绝);`finding_to_comment` 映射 benchmark 注入格式 |
| `pr_review/orchestrator.py` | **ReviewOrchestrator**: 规则兜底 → 三视角 `asyncio.gather` 并行(黑盒)→ 综合层 → **受控追问(≤2 轮/视角, 触发=高严重度候选被丢弃或视角自报矛盾)**→ 终结;分发器可注入 |
| `pr_review/runtime_dispatcher.py` | 生产分发器: 每视角独立 `FindingRuntimeBridge`(agent_type=`review:<视角>`)+ `build_review_perspective_spec` + recon_payload 组装 + review 专属 nudge/finalizer 文案 |

### 底座最小参数化(spec §3.4"先参数化,再替换")

| 位置 | 改动 |
|---|---|
| `runtime_core/runtime_tool_registry.py` | ① `review:*` 分支自动挂 FinalizeReview;② `tool_allowlist` 可选参数(权限矩阵过滤,终点工具始终保留);默认行为不变 |
| `review_runtime/query_loop.py` | 终点工具名收敛为模块常量 `TERMINAL_TOOL_NAMES`(+FinalizeReview);bridge 层 `finalizer_tools` 本就全参数化 |
| `review_runtime/bridge.py` | `agent_type` 全参数化(6 处 model_client + `_build_tool_registry`, 默认 "finding" 兼容);`run()` 增加 `finalizer_prompts/finalizer_tools/terminal_action_nudge_message` 覆盖参数 |
| `review_runtime/models.py` | `RuntimeTerminalAction` 增加 `FINALIZE_REVIEW` 成员 |

**文案策略说明(spec §3.4.4 的执行口径)**:共享 QueryLoop/bridge 的 finding 域文案(nudge/恢复词表)**不在本阶段批量替换**——它们仍被 finding 会话消费;review 路径经 `run()` 新参数传入 review 专属文案,语义已隔离。批量文案替换随阶段 04(finding 域退役)执行。

## 2. 协作协议实现(§3.2)

- **黑盒并行**:`asyncio.gather` 三视角,独立 session,互不通信(test_review_parallel 验证 max_inflight==3)
- **TaskHandoff 唯一回传**:from_agent=视角;key_findings 每项带 `source`(spec 03 归因)
- **受控追问**:同 session 续跑(`continue_session_until_payload` 机制),只传结构化事实;≤2 轮强制停止(test_guided_followup)
- **信息边界**:追问消息由 `build_followup_prompt` 生成,仅含 评论字段+证据引用(test_no_cross_perspective_chat 断言无 reasoning/transcript 透传)

## 3. 引擎与入口

- **CLI/API 默认 `engine=rules`**(全离线):规则层→综合层→评论 JSON,零 LLM 依赖
- **`engine=runtime`**:`run_review_pipeline_async` → 三视角 LLM 编排;`--engine runtime` / `POST /pr-reviews {"engine":"runtime"}`
- 测试驱动装置 `tests/pr_review/fake_runtime.py`(源自 spike):ScriptedLLMService 驱动**真实 QueryLoop** 验证终结/nudge 链路

## 4. 测试(spec §6 全覆盖, 44 用例)

finalize_review(拒绝/成功/空评论/路径逃逸/真实 QueryLoop 终结)· terminal_nudge(nudge×2→incomplete; 预算内终结)· review_parallel(并发/去重/保留)· handoff_contract · guided_followup(触发+收敛; ≤2 上限)· no_cross_perspective_chat · synthesizer_priority · comment_on_added_lines · rules_engine(命中/误报/扩充/CLI E2E)· context_before_dispatch · agent_permission_matrix · 全量回归 178 绿

## 5. 边界条件落地(§7)

无 diff→空评论集(`empty_reason="no_diff"`)· 落行违规拒绝计数(`rejected_off_diff`)· CI 缺失→None 不阻塞· Verify 失败→`needs_verification=true` 标记(Verification Agent 沙箱门禁留待阶段 03 评测闭环接入)· 规则层独立出审(LLM 不可用兜底)
