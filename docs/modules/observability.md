# CodeSage OpenTelemetry / Phoenix

## 固定版本与语义

- OpenTelemetry API/SDK/OTLP HTTP exporter：`1.44.0`。
- OpenInference semantic conventions：`0.1.35`；所有 OpenInference Trace 仍是标准 OTLP Trace。
- Phoenix：`arizephoenix/phoenix:version-20.8.0`，本地 SQLite volume；UI 与 OTLP HTTP 均绑定 `127.0.0.1:6006`。
- CodeSage 扩展属性使用 `codesage.*`；模型、工具和 Agent 类型用 OpenInference `LLM/TOOL/AGENT/CHAIN`。

## 启动

```powershell
docker compose -f deploy/observability/docker-compose.yml --profile observability up -d --wait
$env:OTEL_ENABLED = "true"
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:6006/v1/traces"
```

API 的 `service.name` 为 `codesage-api`，Worker 为 `codesage-worker`。在浏览器打开 `http://127.0.0.1:6006` 查看 Trace。关闭 Phoenix 或 OTLP 请求超时只会产生诊断日志；业务提交和恢复不依赖 Phoenix。

## 隐私与完整性

- 默认 `OTEL_CAPTURE_CONTENT=false`，不导出 prompt 或源码正文。
- 属性在本地 JSONL 与 OTLP exporter 之前统一脱敏；Authorization、API key、token、URL 用户信息会清除，单值受 `OTEL_CAPTURE_MAX_BYTES` 限制。
- 本地 `.auditai/observability/traces.otlp.jsonl` 每行是标准 `ExportTraceServiceRequest` 的 Proto JSON 表达，不是自定义事件协议。
- Trace 允许采样或因硬杀丢失。恢复事实仍来自 lease、checkpoint、stage、finding 和 tool receipt；不能用 Trace 恢复权限或业务状态。

## Provider 能力矩阵

| 能力 | LiteLLM adapter | 其他 adapter |
|---|---|---|
| 逻辑 model attempt | 支持 | 支持 |
| 单次 CodeSage provider request | 支持 | 支持 |
| adapter 内部 HTTP 重试次数 | 未稳定暴露，标 `request_count_unknown` | 未验证 |
| provider 返回 token usage | 有字段时精确记录，缺失为 null | 有字段时记录 |
| cache read/write token | provider 明确返回时记录，不估算 | 未验证 |
| TTFE | 首个可见 content/reasoning/tool/done 事件 | 流式事件可用时 |

不把一次 adapter 调用宣称为一次真实 HTTP 请求；费用聚合只使用带 provider usage 的 `provider.request` Span。

## 验收状态（2026-09-09）

本地标准 OTLP JSONL、W3C queue carrier、脱敏、exporter 故障隔离、QueryLoop/工具/Session/queue 回归与独立迁移升降级均已通过。确定性双 Worker 验收为 32 passed、1 skipped。

Phoenix compose 的配置和固定版本已经交付，但当前 Docker daemon 通过 `docker.xuanyuan.me` 拉取镜像时在 manifest HEAD 返回 403，容器未启动，因此尚未宣称 Phoenix 实机入库通过。网络恢复后执行：

```powershell
docker compose -f deploy/observability/docker-compose.yml --profile observability pull
docker compose -f deploy/observability/docker-compose.yml --profile observability up -d --wait
```
