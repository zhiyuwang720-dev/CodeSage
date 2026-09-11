# Plan 20P0：先收敛模型层，再建设观测闭环

版本 2.0，2026-09-10。状态：已制定，尚未实施或验收。

配套：[执行规格](../spec/20P0-LiteLLM模型边界与迁移验收.md)、[Plan 20](20-修复%20CodeSage%20观测真实性，完成可用于%20Harness%20优化的诊断闭环.md)、[统一进度台账](../spec/20-实施进度与完成情况.md)。

## 1. 决策、目标与范围

CodeSage 只保留一条 LiteLLM SDK 模型调用路径。厂商协议、响应解析及通用模型异常使用 SDK；CodeSage 保留配置快照、准入/取消/deadline、Harness 消息转换、业务恢复、usage 来源与运行关联。

统一的是 OpenAI 风格的 SDK 请求接口，不要求 Anthropic 等厂商都提供 OpenAI 网络协议。默认采用异步 completion/stream 调用；现有 endpoint 必须通过显式配置映射保留语义，不能改 endpoint 或偷偷更换模型来跑通测试。

本轮不部署 LiteLLM Proxy，不引入 Router、fallback 模型、额外服务、API key 管理平台，不重写 QueryLoop、SessionStore、工具网关或审查流程。生产配置、提示词、模型参数及实际工具内容不作无关变化。

前置完成后，Plan 20 不再为每个旧 adapter 实现 provider instrumentation，不再维护自研模型计费引擎。LiteLLM 负责模型观测，项目统一 OTel 基础设施负责导出；业务 Trace、工具观测、结构化日志与离线报告继续实施。

## 2. 源码基线与现有工作处置

2026-09-10 文档勘察 HEAD：`e6b5d2056d3555ce0d3cd651eb07c55a412353a0`。实际开工必须重新记录 HEAD、dirty 清单和脱敏配置快照。下列是当时可见源码事实，不是功能通过证明。

| 位置 | 事实 | 处置 |
|---|---|---|
| `execution_plane/models/factory.py` | 按 endpoint/provider 路由多个 adapter，并缓存实例 | 替换为 SDK 配置解析；不保留 adapter 插件体系 |
| `models/adapters/*`、`protocols/*`、`base_adapter.py` | 存在自研协议及 SDK 双重适配 | 迁移完所有调用者后删除，不只是改名搬家 |
| `models/service.py`、`retry.py` | service 和 adapter 均有重试；流式另有 QueryLoop 所有权 | 按 Spec 明确重试预算，移除重复 transport retry |
| `models/adapters/litellm_adapter.py` | 请求期间修改 `litellm.cache` / `drop_params` 全局变量 | 改启动期及每请求明确配置；并发不得互相改配置 |
| `models/errors.py` | 模型异常与工具异常/恢复策略混杂 | 删除重复模型异常；有调用者的业务错误迁到所属层 |
| `models/types.py`、`usage.py`、contracts、bridge | W01 已加入来源/缺失/估算及透传 | 保留语义与有效测试，迁移后重验；不预先宣称新路径已通过 |
| `infrastructure/observability/llm_semantics.py` | 已有字段映射 | 保留必要标准补充，不能再生成第二份计费 span |
| `provider_requests.py` 与 Anthropic adapter 的 dirty 修改 | W02 自研请求观察器正在接入 | 暂停扩展；先留存差异，改由 SDK integration 接管后删除重复部分 |
| `api/v1/endpoints/config.py` | 配置 API 依赖 LLMFactory 元数据和协议列表 | 同步配置 DTO/连接测试/前端选项，不遗漏非 PR 路径 |
| `control_plane/review_policy.py` | 恢复指纹包含协议和工具格式 | 加实现语义版本；不兼容旧 checkpoint 必须明确拒绝 |
| `backend/pyproject.toml` | `litellm>=1.0.0` | 选择并精确锁定通过能力验证的版本，记录传递依赖 |

原进度台账的 W00/W01 测试记录作为历史保留。不得把历史通过移植成新版本通过，也不要求全量回退 W01。未经引用检索，不预设 tokenizer、memory_compressor、prompt_cache 都可以删除。

## 3. 执行步骤

### P0.1 基线与能力验证，先不替换生产入口

1. 保存当前实现差异、测试失败、依赖版本和调用图，不读取/输出密钥。
2. 检索所有绝对及相对 imports、重导出、动态构造和测试引用；覆盖 review、finalizer、compaction、judge、配置连接测试、prompts、audit sessions。
3. 建立真实 LiteLLM SDK + 本地进程外 HTTP/SSE fixture，禁止 mock SDK 发送。
4. 核实 SDK 回调和 OTel integration：请求级、deployment attempt 级、底层 HTTP 重试级不能混淆。记录源码文件/符号、版本、实际回调顺序。
5. 必测 DeepSeek 当前配置语义、OpenAI-compatible、Anthropic；其余旧选项要么完成映射，要么明确停用并报告，不能保留一个隐蔽旧 adapter 兜底。

产物：能力矩阵、调用者清单、旧新配置映射、旧新重试预算表、AP01—AP05 证据。若 SDK 无法观察必要事实，先提交具体差距与最小 hook 方案，不重新造厂商客户端。

### P0.2 收敛模型调用和配置

1. 保留 `LLMService` 作为过渡薄外观，转调唯一 SDK 客户端；禁止 service→factory→adapter→SDK 的旧链路继续存在。
2. 统一配置解析为不可变请求快照，真实模型/endpoint 与 `review:security` 等业务别名分开。
3. 消息转换只从 Harness transcript 到统一 SDK schema；tool IDs、reasoning 字段和流式事件保持语义。
4. 参数不支持时清晰失败，不使用全局 `drop_params=True` 静默忽略。必要厂商参数通过 SDK 文档配置传入，不手写协议 body。
5. 迁移连接测试和所有辅助调用；配置界面必要改动属于本轮，不进行 UI 改版。

产物：SDK 客户端、薄 service、配置/消息转换与调用者迁移，AP06—AP09。

### P0.3 重试、异常、缓存和恢复

1. Harness 管理的调用关闭 SDK/底层 SDK 自动重试，保留 QueryLoop 的原业务尝试上限。避免服务层与 SDK 倍增。独立非 Harness 调用使用 SDK 有界重试，默认最多 3 次实际请求，必须通过 fixture 验证。
2. 新旧上限不同必须在预算表列明，这是本轮允许的明确行为调整；不要求保留历史意外乘法重试。
3. 只把 SDK 异常映射到现有 RuntimeStopReason/恢复类别；不复制异常类继承树、HTTP 文本解析或通用退避框架。
4. 保留流中断的 transcript 修复、工具去重、取消及 deadline；未闭合工具参数不能执行。
5. 响应缓存默认关闭；提供商 prompt cache 经 SDK 明确配置；token 预算估算与上下文压缩继续在 Harness 执行，估算不能进入实测费用。
6. fingerprint 纳入模型边界实现版本与影响语义的快照字段；旧版本不兼容拒绝复用，绝不批量改写历史身份。

产物：AP10—AP12 以及既有恢复回归。

### P0.4 观测边界交接与删除

1. LiteLLM 官方 OTel integration 负责模型 span；沿用统一 TracerProvider 和 exporter，不启两个全局 provider。
2. 在 SDK 支持的 hook 中补业务 metadata、内容引用和缺失来源。不得再创建外包同次调用的第二个 LLM span。
3. 成本计算使用锁定 LiteLLM 计算能力和冻结价格输入；本地代码负责价格来源、匹配与缺失校验、结果聚合，不复制模型定价公式库。
4. 删除旧 adapter/protocol/base、重复 retry/error/cache 分支；有用业务逻辑迁到所属模块。依赖仅在无引用且非 SDK 必要依赖时删除。
5. 回归并检查净代码量、依赖方向、旧符号零生产引用，更新能力清单及操作文档。

产物：AP13—AP15、删除清单、当前 commit 的 P0 签收。

## 4. 阶段门与交接

- P0-M0：能力矩阵、重试预算、迁移范围有证据；允许开始替换。
- P0-M1：统一 SDK 路径和所有入口迁移；取消、流中断、完成视角复用通过。
- P0-M2：旧适配器删除、SDK 观测接到统一 OTel、相关回归无新增失败；允许 Plan 20 W02 继续。
- Plan 20 的工具/日志等独立任务可以准备，但整体验收必须在 P0-M2 后重跑。

完成标准：AP01—AP15 全部有当前版本证据；支持列表真实；生产路径不依赖旧 adapter；没有全局配置串扰和倍增重试；已完成阶段仍复用；没有重复计费 span。

本前置自动验收不调用付费模型，不能声称供应商真实服务已验证。Plan 20 L3 另行显式执行。

## 5. 实施约束与回退

按能力验证→客户端→调用者→恢复→观测→删除拆可审查提交，禁止直接 reset/revert 当前用户工作区。需要替换正在开发的 W02 时记录文件差异及处置理由。

只有本任务自己生成且无后续依赖的提交才可设计反向补丁；实际回退先检查 dirty 冲突，不回退业务数据或迁移。出现 SDK 核心能力缺口时停在该阶段并提交最小诊断，不能以保留永久双栈宣称收敛完成。

建议有效工作量 4—7 日，取决于现有非 PR 调用者及协议配置数量；这是实施估计，不是验收期限。
