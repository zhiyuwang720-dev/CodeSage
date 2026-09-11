# Spec 20P0：LiteLLM 模型边界与迁移验收

版本 2.0，2026-09-10。状态：未实施。配套 [Plan 20P0](../plan/20P0-LiteLLM模型层收敛.md)、[Spec 20](20-观测真实性与诊断闭环.md)、[台账](20-实施进度与完成情况.md)。

本规格定义前置要求 P01—P12、测试 AP01—AP15。不得将历史 W01 PASS 当作本规格 PASS。与旧版 Spec 20 的 adapter 埋点/多层重试要求冲突时，以本版本的替代条款为准；未变更的内容、权限、可靠性要求继续生效。

## 1. P01：唯一模型出口及依赖规则

允许依赖：调用者 → 薄 `LLMService` → SDK client → LiteLLM SDK。RuntimeBridge 将 Harness transcript 转成 SDK 输入，再把标准响应/增量转换为既有 runtime event。基础设施层安装 LiteLLM callback/OTel integration，业务层只传关联 metadata。

建议最小布局（可复用同职责旧文件，不能为了目录形状另造抽象）：

| 模块 | 允许职责 | 禁止职责 |
|---|---|---|
| `execution_plane/models/service.py` | 兼容调用外观、准入/deadline协调 | adapter选择、重试循环、价格计算、OTLP导出 |
| `execution_plane/models/client.py`（新增） | 唯一 `acompletion` 调用、流生命周期、标准响应转 runtime 输入 | 手写厂商HTTP/SSE协议、多种client基类 |
| `execution_plane/models/config.py`（新增） | settings解析、模型映射、有效参数快照 | 在线探测偷偷改模型、保存密钥到日志 |
| `execution_plane/models/types.py`、`usage.py` | 必要业务类型、来源完整性、SDK usage薄归一 | 再实现所有厂商响应协议 |
| `execution_plane/runtime/bridge.py` | transcript/tool-call对应关系与流式事件转换 | endpoint厂商分派 |
| `infrastructure/observability/litellm_integration.py`（新增） | SDK callback安装、上下文、标准字段补齐、内容采集交接 | 新Trace协议、业务事务、自研模型网关 |

不得在 callback 中提交 StageResult/Findings、授予工具权限、恢复执行所有权。删除后不得留运行开关切回旧适配器。

## 2. P02：配置快照与支持范围

每次请求解析后形成不可变快照，至少含 `configured_model`、`sdk_model`、`provider`、`endpoint_id`、实际 transport family、请求参数、stream、timeout、purpose、retry_owner、retry_budget、model_boundary_version。密钥只在调用参数中传递，不进入快照可序列化表示或缓存 key 明文。

SDK model 可以带 SDK provider 前缀；provider 实际请求 model 单独记录。业务 `review:security` 不能覆盖模型字段。response_model 不存在时 null，不从业务名称推断。

旧配置的 provider/endpoint_protocol/tool_message_format 必须建立逐项迁移表：旧值、SDK值、预期语义、是否支持、fixture/回归ID。不自动写用户 `.env`。已有受支持配置经解析保持 endpoint、模型和有效参数；不支持配置在发送前返回明确配置错误。不能为了保留旧下拉选项声称某协议支持。

OpenAI-compatible、当前 DeepSeek 配置、Anthropic 为本轮必测 SDK 映射；其他旧选项清晰标 supported 或 unavailable，启动/连接测试失败须可解释。公开 API 的必要字段兼容可做薄投影，旧配置字段不得继续驱动旧适配器。

不支持参数默认报错。确需过滤的参数在配置映射中显式列出理由并测试，禁止默认全局 drop_params。并行请求不能修改 LiteLLM 全局 model/api_base/key/cache/drop_params。

## 3. P03：依赖与能力证据

精确锁定 LiteLLM，经现有依赖机制记录可复现的 SDK 及底层 provider SDK 版本；不能只记录开发机 pip 输出而仍允许任意版本安装。保留已有 OTel/Phoenix 版本，只有证实不兼容时做最小升级并重跑相关回归。

`capability-matrix.json` 每路径包含 SDK版本、provider/transport、SDK调用函数、source_file/symbol、request/stream/usage/cost/callback/content 能力、callback粒度、底层重试设置、验证命令及证据。内部 hook 必须注明 private/version-sensitive，不 monkeypatch SDK 实现作为正式方案。

官方能力参考（不是当前安装版本已验证的证明）：

- [统一SDK接口](https://docs.litellm.ai/)
- [OTel integration](https://docs.litellm.ai/docs/observability/opentelemetry_integration)
- [CustomLogger / callback](https://docs.litellm.ai/docs/observability/custom_callback)

文档的 request-level 与 deployment-level hook 不等价于任意底层 HTTP attempt。每一种粒度必须用服务器收到的实际请求证明。SDK Proxy-only hook 不得用于 SDK 方案。

## 4. P04：请求及流契约

请求消息保持 system/user/assistant/tool 顺序、tool_call_id、tools schema、tool choice（现有调用提供时）、reasoning 可见字段及参数。转换后不能丢弃已有工具结果、复制 system 或重写提示词。Fixture 比较语义JSON；SDK注入的已验证默认参数单独列出。

流式消费必须处理 content/reasoning/tool-call 参数跨多个 chunk、多个工具 index、finish_reason 早于 usage、usage-only 末块、空块、无usage终止、异常和取消。工具 JSON 只有完整验证后才进入 ToolGateway。最终 done 至多一次；error 后不再 done 成功；结束块的 usage 不能按每 chunk 累加。

支持非流式及当前 disable_streaming 路径。tool-call 参数转换使用标准 SDK 数据结构，不继续维护厂商原生事件解析器。provider 可见 reasoning 允许透传；不能伪造不可得内部推理。

取消必须关闭/释放异步流及准入资源，在受控本地挂起服务器场景 10 秒内停止新请求；deadline 包括准入和本轮请求，不因重试重新获得无限时间。

## 5. P05：重试单一所有者

| 场景 | 所有者 | MUST |
|---|---|---|
| Harness review/finalizer/compaction 经QueryLoop管理的调用 | QueryLoop | LiteLLM及底层SDK retry=0；沿用业务恢复上限，无service重试 |
| 未经过QueryLoop的独立模型调用 | LiteLLM SDK | 明确最多3次实际请求（初次+2次重试）；底层SDK不得另加重试 |
| 已输出部分响应/工具参数后失败 | Harness | 不允许SDK透明重放拼接为同一成功响应；记录partial及恢复决策 |
| worker/lease任务恢复 | 控制平面/既有队列机制 | 不计入模型传输retry，也不重跑已完成阶段 |

实际调用者是否经 QueryLoop 由调用图确认，不能仅按 purpose 字符串猜测。SDK 无法约束内部重试或无法中断时 AP 测试失败，不能仅把配置字段写0当作通过。

迁移表记录旧配置最大尝试、实际叠加点、新预算和原因。本轮允许消除原有倍增重试，不修改业务 max_turns/完成门禁来掩盖变化。准入限流仍是执行约束，不因删 retry.py 而删除。

## 6. P06：异常映射与消息恢复

优先使用 LiteLLM 标准异常类型、状态码和已知结构字段；映射到既有 RuntimeStopReason/recoverable_error_kind，不复制 LLMError 继承树。只保留明确的业务配置错误或结果错误，归所属层。不得解析面向人的错误字符串猜 retry-after 或把所有错误标可恢复。

认证/参数错误不重试；受控限流/连接错误按P05；CancelledError 必须保持取消语义，不能改成模型服务失败。流失败保留已知usage与partial内容；transcript不能出现无配对工具结果。恢复沿现有会话/Checkpoint机制，不新增模型层状态库。

## 7. P07：缓存、token与usage

响应缓存默认关闭，测试必须证明重复调用确实命中HTTP服务器。提供商 prompt cache 只通过 SDK 支持参数配置，不自行修改系统提示词来模拟缓存；若旧缓存前缀策略影响实际模型输入，记录差异并由相同审查fixture验证。

tokenizer只承担预算估算；memory_compressor/上下文压缩若有业务引用，迁移到Harness对应职责后保留。无引用才删除。每个缓存/压缩旧模块必须在删除清单记录直接/相对引用检索结果。

保留W01的 nullable usage、field_sources、estimated_usage、anomalies。SDK已归一化数据不能再次套原生Anthropic等公式重复加缓存。定义数据来源层 `provider_raw|sdk_normalized` 与 normalizer版本；来源不可证实时保持unknown，不按provider名称猜。

SDK合成零与原始明确零必须分清；hook拿不到原始证据时不能标provider。成本字段缺失保持null；估算用于预算但不写标准实测token。标准字段补齐不得导致父/子 span 重复计费。

## 8. P08：SDK观测接入

默认使用 LiteLLM 官方 OTel integration 产生模型span；安装到CodeSage统一bootstrap，复用当前TracerProvider/resource/exporter。启动只注册一次，不在请求中重配callbacks。必须在锁定版本验证如何复用provider及active context。

CodeSage不再自行调用observe_provider_request包住同次SDK请求创建第二个LLM span。允许一个薄CustomLogger补运行ID、purpose、字段来源、内容引用与本地请求记录；不得同时另装自动OpenAI instrumentation重复生成同级计费span。

模型span名称采用SDK实际名称，不要求硬改为provider.request。导出归一投影仍提供稳定provider_request_id；span/callback/request按关联ID去重，不能靠模型名+时间猜配对。

一次逻辑 SDK 调用如果包含多个实际请求：尝试记录与逻辑回调区分；只对已知实际调用计费一次。官方integration不能提供逐尝试span时，先验证关闭隐式retry/采用既有Harness重试是否解决；仍有缺口可在公开per-attempt hook实现最小补充，但必须关闭同次请求的重复LLM计费输出并记录ADR，不能构建双重模型span系统。

内容开关、脱敏必须覆盖SDK原生log/input/output，不允许项目exporter脱敏前SDK已把秘密发送外部。capture=false既无本地正文也无OTLP正文。SDK内部debug/原始日志默认关。callbacks异步、失败不影响业务，资源关闭/flush统一总时限沿Spec20。

## 9. P09：成本边界

LiteLLM callback `response_cost` 是候选计算结果，不自动证明usage完整、价格匹配或最终账单。仅当usage来源与冻结价格匹配通过时接受；否则状态unknown/partial并保留原因。

使用锁定SDK计算能力及显式价格输入作为唯一模型价格计算路径；自定义网关价格使用其支持的注册/配置能力。项目只做严格价格选择、冻结快照、SDK结果校验及Decimal聚合，不另写厂商计费引擎。SDK用浮点计算时保存原值和规定比较容差，不承诺Decimal逐位一致。

价格快照包含provider、endpoint、model、币种、生效期、缓存类别、单位、来源、SDK版本/价格表hash、显式覆盖。未知模型不得用近似字符串套价格，SDK默认零不得当免费。Phoenix与本地须使用同一冻结来源；其能力缺口在Plan20价格验收处理。

## 10. P10：API、配置和恢复迁移

全仓检索包括 `__init__.py` 重导出、测试、脚本、前端 provider元数据。连接测试必须调用新出口；不能一个按钮验证旧adapter而审查走新SDK。

保持任务API及SSE既有消费语义。必要配置选项变更同步DTO、前端类型和帮助说明。不能以“本轮不改前端”为由留下失效协议选项。

恢复指纹纳入 `model_boundary_version=litellm_sdk_v1` 及有效模型/消息格式/参数/重试策略版本；凭据排除。旧身份不改写、不能在resume偷偷重新生成。缺版本或不一致时明确 `model_execution_incompatible`（可使用既有兼容错误载体）并提示新任务；新路径生成的task跨worker恢复必须成功且已完成阶段不新增请求。

## 11. P11：删除与回归

最终无生产引用：自研 `adapters/*`、`base_adapter`、`protocols` 分派/转换、adapter工厂、模型专用通用retry框架。移除旧模块前补新路径语义测试；旧测试中的私有类断言可替换，usage/断流/取消反例必须保留。

`errors.py`、`prompt_cache.py`、`tokenizer.py`、`memory_compressor.py`逐项判断，不能按文件名全删。删除清单含 old symbol、调用者、replacement、删除/迁移理由、验证测试。记录生产/测试分别的增删行数和模块数，不设促使隐藏复杂度的硬行数目标。

禁止在新client里重新堆积provider if/else原生协议实现。没有SDK能力时显式unsupported，不保留静默legacy fallback。

## 12. P12：验收与进度规则

测试ID稳定，使用本地HTTP/SSE服务器+真实SDK；除L0纯函数外不得mock SDK发送。使用已隔离DB/Redis运行AP12，不能用SQLite代替。自动默认不访问付费endpoint。所有源码链接/版本/输出进入证据manifest。

建议统一入口扩展为：

```powershell
./backend/tests/observability_acceptance/run-acceptance.ps1 -Suite model-foundation
```

此命令是待实现交付物，本次文档任务没有运行或证明其存在。包含AP01—AP15，失败1、环境/证据不全2、全部通过0。不得以pytest局部0掩盖未运行用例。全套Plan20 `-Suite all` 后续必须包含此前置组。

| ID / 层次 | 需求 | 操作 | 必须断言及证据 |
|---|---|---|---|
| AP01 L0/静态 | P01,P03,P11 | 输出调用图、依赖锁、删除清单 | 所有生产模型出口可追踪；无旧adapter引用；不能仅检索一个目录 |
| AP02 L1 | P02,P04 | OpenAI-compatible、DeepSeek当前配置映射、Anthropic流/非流 | SDK实际请求模型、endpoint、messages/tools/参数正确；保留server正文及快照hash |
| AP03 L1 | P03,P08 | 记录请求级/attempt级回调与HTTP记录 | 粒度准确、顺序及版本明确；无隐式漏计、不能以success次数代HTTP次数 |
| AP04 L1 | P05 | Harness下两次429后成功；独立调用同场景 | 各实际3次，Harness SDK每次只发1次；auth错误仅1次；预算耗尽无额外请求 |
| AP05 L1 | P07,P09 | 明确0/无usage/SDK合成0/cache/reasoning/失败usage | 来源正确、缓存不重加；未知费用非0；W01有价值用例迁移后通过 |
| AP06 L1 | P02,P08 | 两个不同模型/endpoint请求并发 | 配置、认证和trace互不串；敏感值只用fixture假值；capture=false无正文泄漏 |
| AP07 L1 | P04,P06 | 分片工具参数、多工具、末usage-only、finish早于usage | 完整参数一次交付；done一次；token不按chunk累加 |
| AP08 L1 | P04,P06 | 输出半个工具JSON后断流，再按Harness恢复 | 不执行半工具、不拼接两次响应伪成功；partial可见、消息配对正确 |
| AP09 L0/L1 | P02,P10 | 连接测试、prompts、audit sessions及辅助调用 | 实际走唯一SDK；配置错误发送前失败；前端typecheck；purpose可区分 |
| AP10 L1 | P04,P05 | 挂起流、准入等待时取消/deadline | 10秒内不新增请求；释放许可/流；取消非provider故障、deadline不重置 |
| AP11 L1 | P07 | 同请求两次；开启provider cache配置 | 响应缓存关闭两次HTTP；prompt策略通过SDK表达；未引入偷偷改prompt |
| AP12 L2 | P06,P10 | 新任务完成一个视角后取消换worker恢复；另测旧指纹 | 新任务身份保持、已完成阶段HTTP计数不增；旧身份拒绝且DB不改 |
| AP13 L1/OTel | P08 | 同一调用结束、失败、重复callback、再次初始化 | 单一provider及计费span；关联稳定；重复回调不重复计token/cost |
| AP14 L0/L1 | P09 | 冻结价格、未知价、成功及失败usage | SDK计算固定值与期望在容差内；未知/partial不伪完整；保存版本和价格hash |
| AP15 L0/L1/L2 | P11,P12 | 相关回归、删除扫描、SDK/export故障 | 模型/runtime/session/工具/控制/队列回归无新增失败；观测失败不改结果事务 |

AP02为本地协议验证，不能标真实供应商verified；真实端点留给Plan20 A30。AP12至少一次完整通过；Plan20 A16/A17仍要求三次重复故障验收，不用前置局部结果替代。

证据目录 `backend/.acceptance-artifacts/plan20p0/<UTC>-<commit>/`：manifest、tests、baseline、capability-matrix、callers、config-mapping、retry-budget、deletion-map、HTTP记录、callback记录、OTLP、reconciliation、回归原始输出。内容脱敏，价格明确为测试价。单个测试证据关联REQ/AP及代码hash。

通过前不得将“客户端写完”标P0完成。台账每包状态须有implemented/verified区别；改变SDK版本、重试或消息转换后相关AP和Plan20 A01—A09必须retest_required。
