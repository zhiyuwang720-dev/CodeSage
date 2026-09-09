# Spec 19B：OpenTelemetry 与 Phoenix 观测接入

## 状态与前置

- 状态：代码、迁移与离线 exporter 验收完成；Phoenix 容器实机入库检查被本机 Docker 镜像代理对 manifest 的 HTTP 403 阻塞，待网络环境恢复复验。
- 只采用 OpenTelemetry SDK/OTLP + OpenInference 语义 + Phoenix；禁止自定义 TraceEvent/Trace ID/Span 协议。
- Phoenix/Exporter 故障不能改变业务状态，数据库不能从 Trace 恢复权威状态。

## 组装与传播

- `infrastructure/observability` 集中 OTel Resource、Tracer/Meter provider、OTLP、本地标准 OTLP JSONL exporter、脱敏和 OpenInference 映射。
- `bootstrap` 为 API/Worker/评测进程初始化一次并 bounded flush；库模块不做全局初始化。
- ARQ envelope 仅携带 W3C `traceparent/tracestate`；旧消息兼容并标 propagation missing，不传播 baggage/路径/凭据。
- 默认不导出 prompt/源码正文；公开 fixture profile 可有界 opt-in，并标 bytes/truncated。

## Span 与指标

命令/input/publish → execution attempt → claim/renew/result commit → review/perspective → harness turn → model attempt/admission/provider request、retry backoff、tool invoke。计费 token 只记 provider request span；attempt 不重复记 token。

优先使用 OTel GenAI/OpenInference 属性，扩展属性统一为 `codesage.*`。run/task/delivery/attempt/epoch 等高基数只进 Span/日志，不进 Metrics 标签。

缺失 usage 为 null，provider 明确零才是零；estimated 单列且不进精确费用/cache ratio。累计每个真实 provider request，分 perspective 统计工具与模型；provider/query-loop/ARQ/recovery retry 分层。

## Schema 清理

- 删除 `AuditModelStreamAttempt` ORM、读写、snapshot/API/前端入口并新增 drop migration；保留内存 attempt ID、重试、tombstone、turn/Checkpoint 恢复协议。
- Python `AuditToolCall` 改名 `ToolExecutionReceipt`，物理表名不变；删除持久化 `duration_ms`，API 用 started/completed 近似投影，精确耗时来自 span。
- Phoenix compose 固定版本、localhost、独立 SQLite volume；服务和评测 project 分开。

## 验收

实施证据（2026-09-09）：InMemory/exporter/W3C/脱敏与 exporter 故障隔离 4 项测试通过；QueryLoop 相关 56 项中仅保留既知 nested Write parser 基线失败；tool/session/queue 38 项通过；独立 PostgreSQL migration upgrade→downgrade→upgrade 通过。确定性双 Worker 的完整验收沿用 19A：32 passed、1 skipped。Phoenix 使用固定 `version-20.8.0` compose，但镜像未能越过本机代理 `docker.xuanyuan.me` 的 403，不能把该项记为通过。

InMemorySpanExporter 验证层级、W3C 队列传播、并行隔离、request 去重、usage null/zero/estimated/cache；Phoenix 有界轮询入库；双 Worker 正常/恢复/强杀各三次；exporter 停机不影响提交；schema 升降级仅在隔离库；默认无敏感正文。
