# Spec 19C：Benchmark 评测与可分享报告

## 状态与前置

- 状态：工程实现与自动验收完成；真实模型质量基线、真实 Phoenix experiment 入库和人工 judge 校准待用户显式提供端点/预算后执行。
- 固定 benchmark 输入，经正式控制面和双 ARQ Worker 执行；Phoenix 做 Trace/实验分析，JSONL/HTML 做可移植结果。
- 自动验收只用确定性模型；真实模型必须用户显式开启。

## 数据与隔离

- 只读加载 50 case，忽略旧 codesage 结果；稳定 case/golden/candidate ID。
- prepare 固定 merge-base→head、base tip、diff/source/golden 哈希与 detached clean worktree；无法证明一致则 fixture_unverified。
- golden/judge/第三方答案不得进入模型 workspace；clone 不执行 submodule/安装脚本。
- smoke=排序后前两仓库各一，calibration=每仓库最小/最大 diff，holdout=其余 40，full=50；prepare 后固定列表。
- 产品 PostgreSQL/Redis、Phoenix SQLite 与 runs 目录隔离；评测不新增 ORM。

## Runner 与评分

- `prepare/run/judge/report/compare/all`；`all` 不隐式联网 prepare。
- runner 只经 lifecycle/start/enqueue，两个 production ARQ Worker、每个 concurrency=1、最多两个在途；续跑先查询已登记 task/run，禁止重复付费。
- primary `codesage_matching_v1`：match=true 建边，先最大匹配数、再最大 confidence、再稳定 ID 的一对一匹配。
- secondary `golden_coverage_v1`：golden 至少一条 match 边的覆盖率，不计算 precision。
- judge 显式配置、temperature 0（支持时）、最多重试两次；最终错误为 unknown。人工校准至少 20 对、两轮 uncached 一致率，未完成则 judge_uncalibrated。

## 导出与报告

运行目录包含 manifest/cases/findings/judgments/summary/comparison JSONL、标准 OTel telemetry、单文件 HTML。可靠结果用单 writer/原子替换，坏行阻止认证；遥测坏尾行显式降级完整性。

报告离线、无 CDN、不实现通用 Span 树；public export 删除绝对路径、内部 endpoint、凭据、完整 prompt/source 与私有错误。HTML 内嵌 JSON 转义 script/HTML/Unicode。

## 实施证据（2026-09-09）

- `codesage_eval` 提供 prepare/run/judge/calibrate/report/compare/all/cleanup-legacy；自动测试 20 passed。
- runner 只调用正式 AgentTask create/start/get/findings，最多两个在途；task 创建后先原子登记，恢复不重复 POST；Phoenix experiment 强制 `retries=0`。
- 固定 Git refs、clean 状态、merge-base→head diff 和前后漂移校验；diff_only/full_source 不允许混合比较，fixture_unverified 拒绝成为 run。
- 20 对人工标签校准模板已入库；校准命令做两轮 uncached 判断。当前未提供真实 judge，因此报告状态仍是 `judge_uncalibrated`，不发布质量提升结论。
- 真实付费调用未执行，符合显式 `--allow-model-calls` 约束；工程通过不等同全量质量测评完成。

## 比较与验收

dataset/input/source mode/judge/serializer/评分版本必须一致；模型/提示词/代码是允许变化的实验变量。性能比较还要求并发、硬件、预算、timeout profile 一致，否则 descriptive only。固定 seed、1000 次按 case bootstrap；完整性为硬门禁，高危 TP→FN 仅 needs_review。

哈希保护、workspace 防泄漏、正式双 Worker smoke、Phoenix 映射、评分边界、unknown/失败/遥测/价格缺失、离线重建与前端回归全部通过后，才删除旧 CodeSage 旁路并更新文档。
