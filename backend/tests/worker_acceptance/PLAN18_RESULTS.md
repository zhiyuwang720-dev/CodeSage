# Plan 18 验收结果

执行时间：2026-09-07。基线：`origin/master@6a0324e760ea32f7f184d7740e66534f916d01d8`。

一键命令：

```powershell
powershell -ExecutionPolicy Bypass -File tests/worker_acceptance/run-acceptance.ps1
```

## 结果

- PostgreSQL 16 + Redis 7 真实 Worker 矩阵：31 passed，1 skipped，耗时 508.29 秒。
- runtime/session/PR/控制端相关回归：322 passed，2 failed，耗时 150.88 秒。
- 前端类型检查：`npm run type-check` 通过。
- Alembic：干净 PostgreSQL 数据库从初始版本升级至单一 head `20260907_01` 通过。
- 结构证据：JUnit XML、Worker/PID 日志、模型调用、attempt/epoch、阶段、findings、session/tool call 和数据库 data-only dump 均由一键脚本生成。

本次运行证据目录为 `backend/.acceptance-artifacts/20260907-203014-23996/`（本地验收产物，不纳入 Git）。核心文件：`report.txt`、`acceptance.junit.xml`、`regression.junit.xml`、`database-evidence.sql` 和各 Worker 日志。

## 已通过需求

- 正常非空审查与零发现审查各连续 3 次；非空结果在 DB 中保持 `security/perf/test_gap`，旧 `vulnerability_type` 不参与新写。
- 公开生命周期 cancel/resume 连续 3 次，已完成 security 阶段不重复调用模型。
- 重复投递后 lease 接管连续 3 次。
- 仅一次 enqueue、两个常驻 Worker、杀死 owner 后由 ARQ 原消息自动重试接管连续 3 次；接管耗时约 82 秒，均小于 90 秒；新旧 `attempt_id` 不同。
- report StageResult 故障注入会回滚最终 findings 与 COMPLETED。
- 旧 owner 无法写阶段、非空 findings 或终态；心跳数据库故障会取消并等待 runner。
- 固定 diff/Git 输入、artifact 完整性、恢复身份、AST 依赖方向和 API 薄包装测试通过。

## 跳过、失败与未运行

- skipped：`tests.worker_acceptance.test_review_artifacts::test_store_rejects_symlinked_run_root`。原因：当前 Windows 环境不允许创建测试符号链接；未以 mock 冒充通过。
- 既有失败：`tests.runtime.test_query_loop::test_extract_text_tool_calls_preserves_nested_write_payload`。特征：文本 Write 工具调用的嵌套 JSON 被解析为空 input；与 Plan 18 路径无关。
- 环境失败：`tests.pr_review.test_agent_permission_matrix::test_allowlist_filters_tools`。特征：当前 Windows 仅注册 PowerShell，未注册 Bash；安全、架构与质量视角的可用只读/PowerShell 工具未退化。
- 未运行：全仓非相关业务测试；本报告只声明 Plan 18 相关矩阵。前端类型检查已在一键命令外单独运行。

历史 `RESULTS.md` 保留作为 Plan 16/17 证据，不用新结果覆盖旧报告。
