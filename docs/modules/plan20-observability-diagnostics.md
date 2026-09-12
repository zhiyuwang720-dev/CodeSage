# Plan 20 观测真实性与诊断闭环实施说明

本文记录本次 Plan 20 大修改的架构落点、运维命令、评估测试和已知边界。它是 [CodeSage OpenTelemetry / Phoenix](observability.md) 的 Plan 20 补充说明。

## 1. 本次修改范围

### 模型边界

- LiteLLM SDK 是唯一模型调用路径，唯一发送点仍是 `backend/app/execution_plane/models/client.py::SDKModelClient._send`。
- 关闭远端价格表刷新：`backend/app/core/config.py` 在导入时设置 `LITELLM_LOCAL_MODEL_COST_MAP=true`，避免 Worker 启动和请求阶段联网超时。
- Harness 重试由 QueryLoop 负责；独立调用由薄客户端负责，最多 3 次实际请求。
- `model.attempt`、`provider.request`、`model.admission`、`retry.backoff`、`harness.turn` 已形成业务层级；LiteLLM 官方 integration 只负责真实 provider Span。

### 关联上下文

新增统一 `codesage` correlation context：

- `task_id`、`review_run_id`、`delivery_id`
- `execution_attempt_id`、`lease_epoch`
- `session_id`、`turn_id`、`model_attempt_id`
- `perspective`、`purpose`

标准 `session.id` 使用真实 Harness session；`review_run_id` 聚合多个视角和恢复 attempt。Trace context 通过 W3C carrier 进入 ARQ。

### usage 与 OpenInference

- `LLMUsage` 保留 input/output/total、cache-read/cache-write、reasoning、field sources、estimated usage、anomalies。
- DeepSeek hit/miss、OpenAI cached details、Anthropic cache 字段按已验证包含关系归一。
- LiteLLM 1.98 缺少的 cache/reasoning OpenInference 字段由 `litellm_integration.CodeSageOpenTelemetry` 在 Span 结束前补齐。
- 缺失 usage 保持 missing；SDK 合成零标记 anomaly；估算只进入 `estimated_usage`。

### 内容产物与脱敏

模块：`backend/app/infrastructure/observability/content.py`。

- 默认 `OTEL_CAPTURE_CONTENT=false`。
- true 时捕获模型请求/响应、工具输入/结果/错误、compact 和 finding provenance。
- 文件根：`OTEL_CONTENT_ROOT`，默认 `.codesage/observability/content`。
- 限额：单 Span 预览 32 KiB，单文件 16 MiB，单 run 256 MiB。
- 内容先脱敏后写文件和导出；支持 JSON 字段、URL userinfo、secret query、Bearer/sk 类凭据。
- 每个 artifact 有 sha256、大小、相对路径、capture status；完整性读取失败返回 409。
- 受权下载接口：`GET /api/v1/agent-tasks/{task_id}/diagnostics/artifacts/{artifact_id}`。

### Metrics、日志和导出

- 集中式仪器目录：`backend/app/infrastructure/observability/metrics.py`。
- 已覆盖 task/execution/model/tool/turn/retry/token/compaction 和 telemetry 指标，并提供 cohort、速率、重启差分、nearest-rank 百分位。
- JSON 日志：`backend/app/infrastructure/observability/logging.py`，每进程独立文件、20 MiB × 5 轮转、UTC 时间和关联字段。
- 本地 Trace 文件改为 `BatchSpanProcessor`，OTLP 也使用 Batch；正常退出统一 flush。
- 已实现价格目录：`backend/app/infrastructure/observability/pricing.py`，包含严格 provider/model/endpoint/effective interval 匹配和 Decimal 聚合辅助。

## 2. 常用命令

### 完整自动验收

```powershell
cd backend
./tests/observability_acceptance/run-acceptance.ps1 -Suite model-foundation
./tests/observability_acceptance/run-acceptance.ps1 -Suite unit
./tests/observability_acceptance/run-acceptance.ps1 -Suite integration
./tests/observability_acceptance/run-acceptance.ps1 -Suite all
```

`all` 包含 P0 model-foundation、Plan20 unit、真实 PostgreSQL/Redis 双 Worker 和回归；默认不调用付费模型。

### 诊断 CLI

```powershell
python -m app.diagnostics summarize --input <bundle-dir>

python -m app.diagnostics pricing doctor --catalog prices.jsonl --provider <provider> --endpoint-id <endpoint> --model <model>
python -m app.diagnostics pricing sync --catalog prices.jsonl --output phoenix-prices.jsonl
```

导出命令需要本地持久化读取和业务环境；离线 `summarize` 不访问 DB、网络或模型。

### 显式付费 smoke

```powershell
python -m app.diagnostics smoke `
  --case-id deepseek-public-v1 `
  --allow-paid `
  --max-cost 0.02 `
  --currency USD
```

未提供 `--allow-paid` 时命令拒绝执行。真实价格未知时内容/token 可验证，但成本项必须保持 unknown。

## 3. 评估测试映射

| 测试 ID | 层次 | 主要文件 | 覆盖内容 |
|---|---|---|---|
| A01 | L1 | `test_a01_wire_payload.py` | 最终请求 body、tools、流式/非流式、真实模型字段 |
| A02 | L0/L1 | `test_a02_retry_and_context.py` | 上下文属性、真实 HTTP 计数、retry/backoff/attempt Span |
| A03-A07 | L0/L1 | `test_a03_a07_usage.py` | usage 分层、缺失/零、缓存、reasoning、Anthropic/OpenAI/DeepSeek |
| A08-A09 | L0 | `test_a08_a09_pricing.py` | 0.00104 oracle、有效区间、冲突、严格匹配、价格目录 hash |
| A10-A13 | L0/L1 | `test_a10_a13_content.py` | capture 开关、脱敏、限额、quota、完整性、死锁回归 |
| A19 | L0 | `test_a10_a13_content.py` | finding filter/merge provenance |
| A20-A22 | L0/L1 | `test_a20_a22_metrics.py` | counter 差分、重启、cohort、生成速度、百分位 |
| A23-A25 | L0 | `test_a23_a25_logging.py` | JSON 字段、轮转、Batch exporter、Worker bootstrap |
| A26-A28 | L0/L1 | `test_a26_a28_diagnostics.py` | bundle、离线摘要、public 脱敏、CLI |
| A30 | preflight | `test_a30_smoke.py` | `--allow-paid`、固定 case、预算门 |
| A10/C02 | L2 | `test_a10_live_capture.py` + `a10_capture_probe.py` | 真实 LiteLLM SDK 路径下 `model_request`/`model_response` 内容 caught：Span `capture_status=captured`、artifact 可校验 |
| A26 | L2 | `test_a26_phoenix_pagination.py` | 真实 Phoenix 游标分页 `limit=3` 穷尽去重、批注入 15 Span |
| A29 | L2 | `test_a29_observation_overhead.py` + `a29_benchmark.py` | 观测 off/on 各 3 次确定性测量的原始记录，不断言提速门槛 |

最终干净 commit 的 `-Suite all` 证据位于：

`backend/.acceptance-artifacts/plan20/20260911T165646Z-ea8b2605/`

真实 smoke 脱敏结果位于：

`backend/.acceptance-artifacts/plan20/20260912T023716Z-c6c938d0-l3/`（首轮，响应内容捕获失败）
`backend/.acceptance-artifacts/plan20/20260912T033000Z-worktree-l3/`（修复后复验，Phoenix `model_response.capture_status=captured`）

本地 Phoenix 与 A29 证据：

- A26 分页：`backend/.acceptance-artifacts/plan20/phoenix-local/evidence/a26/phoenix_pagination.json`
- A29 开销：`backend/.acceptance-artifacts/plan20/a29-local/evidence/a29/summary.json`
- Plan 20 全量套件：`backend/.acceptance-artifacts/plan20/suite-local/`

## 4. 当前已知边界

- 当前真实网关模型：`DeepSeek-V4-Flash-0731`。
- 真实 smoke 已验证内容与 usage：97 input / 40 output / 137 total；返回内容 `CODESAGE_SMOKE_OK`。
- 价格来源调查（2026-09-12）结论：网关 `/v1/models` 只有 `id/object/created/owned_by`，无价格字段；`/model/info`、`/model_group/info`、`/spend/calculate`、`/cost/estimate` 对当前虚拟 key 均返回 403（仅允许 `llm_api_routes`）；`/public/litellm_model_cost_map` 是公开通用表，只有第三方 provider 键，没有 Paratera 专属 `deepseek-v4-flash-0731` 价格。响应头也没有 `x-litellm-response-cost`，只有累计 `x-litellm-key-spend`，无法用于单次核验。
- 因此本地暂不写入 `prices.jsonl`，避免编造价格；成本状态保持 `price_pending`。拿到可信价格来源后，用 `python -m app.diagnostics pricing doctor --catalog prices.jsonl --provider openai --endpoint-id https://llmapi.paratera.com/v1 --model DeepSeek-V4-Flash-0731` 校验，再重跑 smoke 完成成本验收。
- Phoenix 已在根 compose 的 `codesage-phoenix-1`（`http://127.0.0.1:6006`）复验：真实 smoke Trace 可见单一 `litellm_request` Span，OpenInference usage 字段齐全，`model_request`/`model_response` 均为 `captured`；A26 分页对 15 Span 穷尽去重通过。
- A29 已执行完毕：off/on 各 3 次 × 20 次真实 SDK 调用；只保留原始测量，不声明提速门槛。
- 未启用 provider 分支保持 `unverified`，不得宣称全 provider 支持。
