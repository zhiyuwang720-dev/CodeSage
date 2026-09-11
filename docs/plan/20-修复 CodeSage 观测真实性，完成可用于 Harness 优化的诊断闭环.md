# Plan 20：基于 LiteLLM 的观测真实性与 Harness 诊断闭环

版本 2.0，2026-09-10。前置：[Plan 20P0](20P0-LiteLLM模型层收敛.md)及[前置规格](../spec/20P0-LiteLLM模型边界与迁移验收.md)。配套[主规格](../spec/20-观测真实性与诊断闭环.md)、[验收矩阵](../spec/20-验收矩阵与证据规范.md)、[进度台账](../spec/20-实施进度与完成情况.md)。

本版替代 1.0 的模型实现方案：先收敛 LiteLLM SDK，再实施模型观测；保留原有业务、工具、指标、日志及诊断验收目标。已有 W01 实现保留语义并迁移复验，W02 自研 adapter 埋点停止扩展。历史进度保存在台账，不自动回退代码。

## 一、目标、范围与实施原则

### 1.1 目标

使一次真实 PR 审查能够回答以下问题：

1. 实际调用了哪个供应商、哪个模型，发送了什么消息和工具定义？
2. 模型返回了什么，每次请求消耗多少输入、输出、缓存 token？
3. 哪个视角、哪一轮、哪个工具耗时或消耗异常？
4. 请求经历了哪些等待、重试、压缩和恢复？
5. 最终发现为什么被保留、过滤或合并？
6. 能否导出完整会话材料，让人或 Agent 在离线环境中定位问题？

最终交付必须同时包括：

- Phoenix 中可理解的 Trace、输入输出、token 和成本。
- 有明确统计口径的 Metrics。
- 会话关联的结构化日志。
- 可离线读取的会话诊断包和 HTML 摘要。
- 真实执行路径的验收证据。

### 1.2 范围

保留现有四层架构、QueryLoop、三个视角、模型配置、提示词与审查规则。

本轮修改集中于：

- LiteLLM SDK 模型观测边界及 usage 透传。
- OpenTelemetry/OpenInference 字段与上下文。
- 工具、压缩、综合结果的诊断信息。
- Metrics、日志、内容产物与导出。
- Phoenix 成本配置、完整性检查和验收。

不新增 Agent、自动路由、Skill 进化、通用事件总线或新的观测数据库。不重建 `AuditModelStreamAttempt` 表，不用 Phoenix 替代业务状态、执行回执和 Checkpoint。

### 1.3 实施原则

- **源码优先**：执行前核实最新工作区，不能根据旧计划假定功能已实现。
- **请求事实优先**：模型名、参数、usage 来自实际调用边界，不能来自视角别名。
- **未知保持未知**：missing、estimated、provider-reported、明确零分别表达。
- **一次请求只计费一次**：逻辑回合、重试父 Span 和供应商请求不能重复携带可计费 token。
- **观测故障不影响业务事务**：但必须暴露观测丢失，不能静默宣称完整。
- **旧数据不伪造修复**：历史 Trace 缺失内容只能标记 reconstructed 或 missing。
- **保留工作区修改**：不重置其他会话正在修改的文件，不自动启动付费批次。

---

## 二、源码基线与已确认问题

旧版问题勘察基于 HEAD `1a8bd8e9`；本版复核 HEAD 为 `e6b5d2056d3555ce0d3cd651eb07c55a412353a0`。下表保留问题来源，W01 已对类型/usage/映射作部分修复，不代表表内每项仍未修改或已经验收。执行者必须记录实际实施时的 commit 和 dirty 状态，并重新确认下列行为。

### 2.1 已确认问题

| 问题 | 当前源码行为 | 修复要求 |
|---|---|---|
| 模型名错误 | QueryLoop 将 `review:security` 等逻辑名称写入请求模型属性；RuntimeBridge 调模型时直接丢弃该名称 | 从实际请求与响应记录模型，视角单独存储 |
| 请求 Span 边界错误 | `provider.request` 装饰 `_collect_model_turn()` | 实际请求由 LiteLLM integration 记录，QueryLoop 只记录逻辑回合 |
| Phoenix token 字段缺失 | `_record_provider_usage()` 主要写 `codesage.usage.*` | 输出 OpenInference 标准 token 字段 |
| 缓存字段丢失 | `LLMUsage` 只有三个总量字段；非流式服务再次只复制三个字段 | 全链路保留缓存及用量来源 |
| 估算伪装实测 | LiteLLMAdapter 缺 usage 或 total=0 时估算，上层把非空 usage 标成 provider | 保留来源，不覆盖明确零，不以估算值冒充实测 |
| missing 变零 | 模型服务缺 usage 时返回全零字典 | 对外兼容处理与观测真实性分开 |
| 内容开关未接通 | `OTEL_CAPTURE_CONTENT` 有定义，未发现内容采集消费路径 | 实际接入请求、响应、工具和内容产物 |
| 工具内容缺失 | `tool.invoke` 主要写工具名、session 和业务状态 | 记录参数、结果、权限判定、错误与内容引用 |
| Metrics 不完整 | 主要只有 token、publish、attempt Counter | 增加轮次、工具、终态、耗时、重试和观测健康指标 |
| 日志不完整 | 仅 basicConfig 和现有 handler 的关联 Filter | JSON 格式、持久化、轮转、会话关联和导出 |
| 截断不透明 | 默认每属性 8 KB，sanitize 丢弃截断标志 | 明确 preview、完整产物、截断与缺失状态 |
| 同步本地导出 | SimpleSpanProcessor 在 Span 结束时写文件 | 有界异步批量导出，记录丢弃与失败 |
| compact 用量口径错误 | compaction 中存在 `len(content)` 写入 output_tokens | 字符数与真实 token 分离 |
| Phoenix 查询不足 | `wait_for_trace()` 请求 limit=10，发现任意 Span 即返回 | 分页读取并核对完整性，不能用“找到 Span”代表闭环 |
| 评测报告缺资源汇总 | 当前 build_summary 主要聚合质量与完整性布尔值 | 接入真实时延、token、成本、重试和日志摘要 |
| Worker 启动差异 | `run_worker()` 构造 Worker 时未传现有 observability startup/shutdown hooks | 两种正式启动方式行为一致 |
| 投递统计不严谨 | enqueue 返回值未检查即增加 success Counter | 区分成功入队、重复 job 和投递失败 |

### 2.2 必须验证、不能先下结论的事项

以下通过源码、已锁定依赖和确定性请求测试确认：

- LiteLLM 及其底层 SDK 是否隐式重试、修改模型名或请求参数。
- P0 迁移前 DeepSeek 实际采用的协议及迁移后对应 LiteLLM transport；新入口不得继续调用 Anthropic adapter。
- 供应商返回的缓存输入是否包含在 prompt 总量内。
- Phoenix 固定镜像 `version-20.8.0` 对成本配置与 session 聚合的实际接口。
- 截图是否来自当前镜像构建；源码中缺失但界面出现的属性不能归因于未核实代码。
- 辅助模型调用，包括 finalizer、compaction，是否全部迁移至唯一 LiteLLM 出口及正确 purpose。

产出一份能力矩阵，列出每条实际使用的 LiteLLM 路径的请求边界、重试所有者、内容捕获、usage 和成本支持情况；P0 已验证能力复用证据，配置/版本变化则重验。未验证路径不得标为已支持。

---

## 三、20A：修复模型事实、内容采集与 Phoenix 成本

### 3.1 建立模型调用的唯一观测边界

保留以下层次：

```text
review.<perspective>
  └─ harness.turn
      └─ model.attempt
          ├─ model.admission
          ├─ provider.request
          └─ retry.backoff
```

具体规则：

- `harness.turn`：一次 Harness 逻辑轮次。
- `model.attempt`：QueryLoop 一次尝试，可包含多个底层请求。
- `model.admission`：semaphore 等待与 provider gap，分别记录耗时。
- `provider.request`：一次被观测到的供应商请求。
- `retry.backoff`：实际重试等待，携带重试发起层。

删除 QueryLoop 上错误命名的 `provider.request` 装饰器及其计费职责。

LiteLLM 官方 OTel integration 负责模型 Span，项目复用统一 TracerProvider 和 exporter；不在 service/adapter 外再包第二个计费 LLM Span。图中的 provider.request 是业务含义，SDK 实际 Span 名称可以保留，由归一投影标识真实请求。

P0 的公开 callback/hook 验证提供请求记录与内容来源。模型请求、deployment attempt 与底层 HTTP attempt 不得混为一层；独立调用的 SDK 重试必须逐次对账。不能逐次观察时优先禁用隐式重试并使用既有 Harness 恢复，不能靠新建厂商适配器解决。

请求内容区分 sdk_input 与 wire_equivalent；本地服务器验证已支持路径的实际请求语义。不能以 callback 包含 messages 就宣称所有协议最终 payload 可见。缺口需要具体源码证据和最小公开 hook 适配；未解决不通过真实请求验收。

重试预算按 Spec 20P0 P05：Harness 单一所有者，独立调用 SDK 有界重试。消除旧乘法重试是已批准的改动，其他业务完成/恢复语义保持。

### 3.2 模型身份与 usage 契约

扩展现有 LLM 类型，不另建平行模型协议。

模型身份至少包含：

- configured/request model。
- response model。
- provider。
- endpoint protocol。
- 经过脱敏的 endpoint identity。
- runtime perspective。
- purpose：review、finalizer、compaction、judge。

usage 至少保留：

- input/output/total。
- cache-read/cache-write。
- reasoning token，供应商提供时记录。
- 来源及字段存在性。
- 供应商原始 usage 的脱敏副本。

归一化要求：

1. 支持 DeepSeek `prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`。
2. 支持 OpenAI-compatible `prompt_tokens_details.cached_tokens`。
3. 支持当前实际启用的 Anthropic 缓存字段。
4. 原始明确零不得触发“自动当作缺失”。
5. 估算结果放独立字段，不覆盖 provider usage。
6. 错误响应已有 usage 时也保留。
7. cache-read 若属于 prompt 总量，不再次加到 total。
8. reasoning 若已包含在 completion，不重复相加。
9. SDK response/callback、薄 service、bridge、RuntimeModelResponse、事件和导出逐层测试，防止中途丢字段。

### 3.3 OpenInference 映射

真实请求 Span 输出：

```text
openinference.span.kind = LLM
llm.model_name
llm.provider
llm.token_count.prompt
llm.token_count.completion
llm.token_count.total
llm.token_count.prompt_details.cache_read
llm.token_count.prompt_details.cache_write
```

同时按锁定的 OTel GenAI 语义映射对应字段，映射逻辑集中管理。

只有 provider-reported 或由已知 provider 字段确定性推导的用量进入标准计费字段。估算用量保留在明确的估算属性中，不能使 Phoenix 展示成实测成本。

外层 Span 可以显示摘要，但不重复写可计费 token。

### 3.4 请求、响应和工具内容

将 `OTEL_CAPTURE_CONTENT` 接入实际采集：

- false：只记录元信息、内容哈希和大小。
- true：记录脱敏后的实际请求、响应和工具内容。
- 本地诊断 profile 显式开启；公共部署默认关闭。
- 不修改现有用户 `.env`，通过独立 profile 启用。

捕获位置：

- 请求：LiteLLM 支持的最终输入 hook；标明 SDK 输入/实际请求等价级别，压缩后的消息必须可查。
- 响应：解析前后可见业务内容，包括 tool calls、finish reason、partial 状态。
- 工具：请求参数、校验后的参数、权限结果、实际返回值。
- compact：触发原因、输入规模、摘要与实际模型用量。
- synthesis：输入 finding IDs、最终保留 IDs、过滤/合并原因。

所有完整内容保存到本地内容产物目录，使用现有 ArtifactRef 能力或其通用文件校验实现，避免另造文件完整性系统。

每份内容关联：

- run、attempt、session、turn、request/tool ID。
- trace/span ID。
- kind、media type、relative path。
- 脱敏后内容哈希、字节数。
- captured、truncated、missing 或 reconstructed。

Phoenix 使用标准 `input.value/output.value`、消息属性及工具参数属性展示有界内容；大内容显示预览和产物引用。

默认限制：

- 单个 Phoenix 内容预览 32 KiB。
- 单个完整内容产物 16 MiB。
- 单 run 内容总量 256 MiB。
- 超限明确标记，不丢失前后大小信息。
- 不保存原始未脱敏副本。

提供受现有身份校验保护的只读内容下载入口，按 run/artifact ID 解析，不接受客户端任意路径。公开报告不依赖该入口。

### 3.5 成本配置

提供版本化本地价格目录及 `pricing doctor/sync` 能力。

价格键至少包含：

- provider/endpoint identity。
- model 或显式 alias。
- currency。
- 生效时间。
- input miss、cache-read、cache-write、output 单价。
- 来源说明。

执行前核实实际 DeepSeek 服务来源；官方 API 使用对应日期官方价格，网关使用网关价格。无法确认时标 price_unknown，不写零。

Phoenix 价格配置和本地报告使用同一价格目录。若当前 Phoenix 版本不支持某种复杂计费表达，明确标记两端口径差异，不声称完全一致。

使用锁定 LiteLLM 的计算能力和冻结价格输入，不新增平行计费引擎。项目保留严格价格匹配、来源/完整性校验、金额 Decimal 聚合；SDK 浮点结果按规定容差核对。回调 response_cost 未知、usage 不全或价格不匹配时，不能当作完整费用。provider cache 的包含关系以已验证数据来源为准，不对 SDK 归一结果重复套原生公式。

### 3.6 20A 验收

- 本地假模型服务收到的请求数与 provider Span 数一致。
- 实际模型为配置模型，视角别名不再出现在计费模型字段。
- 流式、非流式、缓存、明确零、缺失、估算、错误后 usage 全部有测试。
- 重试两次后成功：请求数、失败数、token、费用准确且不重复。
- 请求与工具完整内容可下载、校验，Phoenix 可见预览。
- 使用固定测试价格时，Phoenix 与本地成本可核对。
- 未知价格/usage 显式显示 unknown/partial。
- 模型、提示词、审查规则无无关变化；重试按 P0 已记录的新预算验收。

---

## 四、20B：修复 Trace 语义、Metrics 与结构化日志

### 4.1 上下文与状态

统一关联以下字段：

```text
task_id
review_run_id
delivery_id
execution_attempt_id
lease_epoch
session_id
turn_id
model_attempt_id
provider_request_id
tool_call_id
perspective
eval_run_id / case_id
```

禁止用同一个 `attempt_id` 同时表示 lease attempt 和模型 attempt。

标准 `session.id` 映射到实际 Harness session；`review_run_id` 聚合三个视角和多个恢复 attempt。

标准 Trace context 经 ARQ 传播；恢复建立新的执行 Span，通过业务 ID 和可用 Span Links 关联，不重开已结束 Span。

状态规则：

- 正常完成：业务状态明确，成功 Span 可设置 OK。
- 工具 denied/invalid/failed：设置相应业务状态及 OTel ERROR。
- provider 错误：即使以 error event 返回也设置 ERROR。
- 用户取消：单独 cancelled，不能计入 provider 服务故障。
- `already_owned` 导致的 ARQ Retry：调度退避，不当成执行故障。
- root 业务 failed：不能因为函数正常 return 就保持成功语义。

### 4.2 时延定义

实现以下时点和区间：

| 名称 | 定义 |
|---|---|
| command duration | start/resume 命令接收至本次响应 |
| queue delay | 成功入队至 Worker 接收 |
| delivery-to-claim | 入队至有效领取，包含调度退避 |
| claim duration | claim 操作本身 |
| execution duration | 有效 owner 开始至该 attempt 结束 |
| perspective duration | 当前 attempt 内该视角墙钟时间 |
| admission delay | semaphore 等待和 provider gap |
| provider latency | 实际请求发出至完成/错误 |
| TTFE | 首个内容、推理或工具增量；done-only 不冒充首 token |
| tool duration | 网关入口至结果，分开策略和执行 |
| recovery gap | 最后已知存活证据至接管，注明是观测近似值 |

跨进程时间使用 UTC 并记录 host；同进程耗时使用单调时钟。并行耗时总和不冒充任务墙钟。

### 4.3 Metrics

仪器在初始化时创建一次，固定名称、单位、说明和标签，不在每次调用重新创建。

至少包括：

- task submissions、terminal outcomes。
- execution attempts、lease lost、recovery attempts。
- Harness turns started/completed。
- model requests、errors、retries。
- input/output/cache token。
- tool calls、denials、failures。
- compact 次数及前后字符/token 信息。
- queue、execution、perspective、admission、provider、tool duration Histogram。
- exporter failures、queue drops、content truncations、usage missing。

标签限定 provider、model、purpose、perspective、status、error_kind、tool name 等有限集合。run/session/request ID 只进入 Trace 与日志。

任务完成率以指定提交 cohort 为分母：

- completed / submitted。
- running、cancelled、failed 单列。
- 业务 resume 不创建新任务分母。
- attempt Counter 不用于计算任务完成率。

速率定义：

- token 消耗速率：窗口内确认的 token 增量/秒。
- 工具调用频率：窗口内调用增量/秒。
- 输出生成速度：仅对可测量的流式输出计算；非流式为 N/A。
- 不逐行相加周期导出的累计 Counter。
- 进程重启按 resource instance 区分并处理 Counter reset。

本轮保留标准 OTLP Metrics JSONL，增加确定性窗口聚合与报告，不新增 Metrics 数据库。

### 4.4 日志

统一 Python logging 配置：

- JSON formatter。
- 控制台 JSON 输出。
- 每进程独立滚动 JSONL 文件。
- 每文件 20 MiB、最多 5 个备份。
- handler 初始化与关联字段注入不依赖启动先后。
- API、ARQ CLI、直接 Worker 启动路径保持一致。

基础字段：

```text
schema_version、timestamp、severity、logger、event_name、message
service、process_instance、trace_id、span_id
review_run_id、task_id、execution_attempt_id、lease_epoch
session_id、turn_id、perspective
error_kind、exception、artifact_refs
```

记录关键语义事件：

- task accepted/published/cancelled/resumed。
- owner acquired/lost。
- stage started/reused/completed。
- retry scheduled/exhausted。
- tool denied/failed。
- compact triggered/completed。
- findings filtered/merged。
- terminal committed。
- observability dropped/failed。

不逐 token 写日志，不把完整 prompt 重复复制进每条日志；正文通过内容引用获取。

日志、Span 属性、异常描述、Span Event 和内容产物共用脱敏策略。现有 exporter 只处理 attributes/events 的限制必须补齐，异常 status description 等不能绕过。

### 4.5 导出与运行健康

将同步本地 Span 写入改为有界批量导出：

- 本地 Span exporter 与 OTLP exporter 分别维护失败状态。
- 队列满时不阻塞业务，增加 drop Counter 并输出最小 stderr 诊断。
- 正常退出统一 bounded flush Trace、Metrics、日志和内容队列。
- worker 的直接启动入口也传入 startup/shutdown hooks。
- 每进程独立文件，避免多个进程仅靠 threading.Lock 写同一路径。

硬杀时允许未结束 Span 丢失；不得伪造结束时间或费用。业务状态与已知日志用于说明缺失范围。

### 4.6 20B 验收

- 两个并行 PR、三个并行视角的上下文不串。
- 调度 Retry、用户取消、provider failure、工具 denied 在 Phoenix 中正确区分。
- 指定合成序列可精确算出轮次、工具频率、token 速率、完成率。
- Counter 跨多个快照与进程重启不重复统计。
- 新增 handler、线程/异步任务、两种 Worker 启动方式日志均携带关联字段。
- Phoenix 停机、本地磁盘写失败、队列满不会改变业务终态，但诊断显示不完整。
- 观测开/关各运行三次确定性场景，记录开销；不以“理论异步”代替测量。

---

## 五、20C：会话诊断包、完整性校验与用户可见验收

### 5.1 会话诊断导出

提供两个入口：

```text
diagnostics export --run-id <id> --output <directory>
diagnostics summarize --input <directory>
```

会话包结构：

```text
session-bundle/
  manifest.json
  timeline.jsonl
  logs.jsonl
  model-calls.jsonl
  tool-calls.jsonl
  findings-decisions.jsonl
  metrics-summary.json
  completeness.json
  contents/
  report.html
```

导出规则：

- 以 review_run_id 聚合多个视角、delivery 和 execution attempt。
- 保留原始 trace/span/request/tool ID。
- 时间线包含开始、结束、错误、重试、恢复、压缩与结果处理。
- timeline 是标准遥测及业务事实的分析投影，不成为新的运行时事件协议。
- 不执行导出内容中的任何命令或“建议”。
- `summarize` 是确定性聚合，不默认调用模型。
- Agent 可以读取 JSONL 和内容引用自行分析，不依赖 Phoenix 登录。

提供 local 与 public 两种导出模式。public 移除内部 endpoint、绝对路径、完整源码/prompt 和敏感错误细节，保留可公开指标和案例说明。

### 5.2 完整性

不能继续以“查到至少一个 Span”判断 Trace 完整。

修改 Phoenix 查询适配：

- 分页读取，不限制前 10 条。
- 根据实际 task/run/trace ID 查询并去重。
- 有界等待 exporter flush 和 Phoenix 入库。
- 核对三个视角、已完成 turn、provider requests、工具回执和业务终态。
- 失败时列出缺失项，而非单个 false。

完整性维度至少包括：

- execution。
- trace。
- model usage。
- content。
- logs。
- pricing。
- result provenance。

硬杀产生的未知请求保留 unknown，不能将其作为费用节省。

历史数据库消息重建只能标 reconstructed；不得标记为实际供应商请求正文。

### 5.3 报告与成本核对

扩展已有 benchmark 报告，不重写评分算法：

- 保留质量指标。
- 增加 per-case/per-perspective 耗时、token、已知成本、重试和完成状态。
- 显示采样数、unknown、partial 和截断。
- 区分 review、compaction、finalizer、judge 费用。
- 显示前述速率和完成率口径。
- Phoenix 深链用于详细调试，HTML 不依赖其可用性。

成本一致性测试使用同一冻结价格目录，核对 request → perspective → run → experiment。舍入仅在展示层发生。

### 5.4 确定性集成验收

使用真实 PostgreSQL、Redis、两个 ARQ Worker、真实 QueryLoop/SessionStore/ToolGateway，以及本地假 provider HTTP 服务。不能把模型服务整体替换成直接返回最终 Findings 的 dispatcher。

场景至少包括：

1. 正常多轮：实际调用 Read/Glob/Grep，完整输入输出和 usage。
2. 缓存：cached input 有明确返回，费用计算准确。
3. 非流式：实际模型和 usage 保留，TTFE/生成速度标 N/A。
4. 两次失败后成功：请求数量、backoff、费用和错误正确。
5. 缺 usage：无伪零、无伪实测。
6. 明确零 usage：不自动估算覆盖。
7. 工具 denied/invalid/failed：内容、状态、日志与回执一致。
8. 长内容：Phoenix 预览截断，但完整产物可获取。
9. compact/finalizer：辅助请求不漏计。
10. 取消恢复：completed 视角没有新模型调用，未完成阶段恢复方式明确。
11. 强杀恢复：缺失 Span/usage 明确，不伪造完整账单。
12. Phoenix 不可用：任务正常完成，本地诊断包可导出。
13. 超过十个 Span：Phoenix 查询分页完整。
14. 两个并发任务：日志、内容与 token 不串账。
15. 重复投递：不重复计业务完成或费用。

### 5.5 真实 DeepSeek 验收

自动测试完成后，提供显式真实模型 smoke 命令。命令必须：

- 保留现有模型配置。
- 显示实际 provider、请求模型、协议和价格来源。
- 要求显式 `--allow-paid`，并设置有限 case 数与预算。
- 先运行一个固定公开 fixture，不自动扩展到完整 benchmark。
- 若当前供应商无法确认价格，仍可验证内容与 token，但成本验收标未完成。

真实验收需提交：

- Phoenix 中模型身份、输入输出、token 和成本截图。
- 一个工具 Span 的参数、返回值与权限结果截图。
- 一个会话诊断包。
- 一份指标与成本核对表。
- 对一处实际问题的诊断说明，包含 turn/request/tool 引用。

只展示 Span 树不能作为验收通过。

---

## 六、执行顺序、清理与最终通过标准

### 6.1 提交顺序

0. 完成 Plan 20P0 的模型收敛与 AP01—AP15；删除旧适配路径，签收 P0-M2。
1. 重新确认源码基线、SDK能力矩阵和失败特征测试。
2. usage 契约与真实模型身份。
3. LiteLLM 请求观测、单一计费 Span、重试和等待。
4. 内容捕获、工具内容与 Phoenix 计费。
5. 上下文、状态和 Metrics。
6. JSON 日志、批量导出和观测健康。
7. 会话包、分页完整性、报告。
8. 确定性双 Worker 验收。
9. 显式真实模型 smoke 与结果说明。

### 6.2 清理要求

替代实现通过测试后删除：

- QueryLoop 中错误的 provider-request 计费埋点。
- 重复 token Counter、自研模型计费公式和重复价格计算；SDK只计算一次，父层仅聚合。
- 未接入的内容配置占位代码。
- 将估算标为 provider 的旧分支。
- 仅靠任意 Span 判断完整性的逻辑。
- 只验证 JSONL 形状、却未验证实际语义的重复测试。

保留有价值的反例测试并改为验证修复后的行为，不删除失败来满足验收。

### 6.3 交付报告

记录：

- commit、dirty 状态、依赖及容器版本。
- 实际启用的 LiteLLM provider/transport、SDK版本和回调能力矩阵。
- 修改前后问题清单。
- 自动测试结果与已知基线失败。
- 正常、失败、取消、恢复的证据。
- Phoenix、本地日志、内容产物和导出目录位置。
- 观测开销、丢失边界、未知费用。
- 真实模型验收是否执行及其预算。

### 6.4 最终通过标准

**其他工程师仅拿到一次运行的 Phoenix 链接或会话诊断包，就能还原实际模型输入、输出、工具行为、token、已知费用、耗时和恢复过程，并用具体证据解释一个优化方向。**

若仍需翻数据库、猜实际模型、手工拼 prompt 或把缺失费用当零，本计划不得标记完成。
