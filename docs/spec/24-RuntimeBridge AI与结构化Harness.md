# Spec 24：RuntimeBridge `.ai()` 与 Schema Harness 终结能力

状态：**已批准**

目标基线：`a4595a82d60f`

对应计划：`docs/plan/24-RuntimeBridge AI与结构化Harness.md`

最后修订：2026-09-21

---

# 1. 目标

本 Spec 负责：

* 冻结 RuntimeBridge 层的 `.ai()` 单次调用契约。
* 冻结 RuntimeBridge 层的 `.harness()` 多轮调用、动态终结工具与 payload 提取契约。
* 将通用 Agent JSON 解析器迁移到 `app.utils`，消除临时 harness 目录。
* 为后续 PR-AF orchestrator、CLI 和 AACR 接入建立稳定底层接口。

本 Spec **不负责**：

* 迁移 PR-AF orchestrator 或业务 prompt。
* 接入 CLI、AACR 数据集、前端报告或成本聚合。
* 修改 LiteLLM model service/client 或引入 provider-native structured output。
* 改变旧快速审查的 `FinalReviewPayload`、`FinalizeReviewTool` 或三视角 orchestrator。

完成后必须能够明确回答：

```text
`.ai()` 的 schema、无 schema、错误和成本语义是什么？
`.harness()` 如何在不复制主循环的情况下替换工具注册表和 payload extractor？
动态 FinalizeReview 如何与旧快速审查 FinalizeReview 隔离？
bare mode 的 skill/memory 边界是什么？
```

---

# 2. 强制决策

## 2.1 `AIResult` 是 schema 代理，不是新业务模型

**决策：**

`AIResult` 位于 `backend/app/execution_plane/models/runtime_ai.py`。有 schema 时，实例的未定义属性代理到
pydantic 实例；同时提供 `text`、`content`、`raw_text`、`parsed`、`usage`、`cost_usd`、`model_dump()` 和
`model_dump_json()`。

**原因：**

PR-AF 既会直接读取 `gate_result.confident`，也会在无 schema 时读取 `.text` / `.content`；Bridge 需要兼容两种调用形态。

**允许：**

```text
result.schema_field
result.text
result.content
result.parsed.model_dump()
result.model_dump(mode="json")
```

**禁止：**

```text
伪造 usage 或 cost
在 AIResult 中承载 findings、severity、review 等业务字段
把 schema 校验失败降级成普通 dict
```

**影响：**

后续 PR-AF reasoner 可直接按 schema 字段消费结果，不需要知道 LiteLLM response shape。

## 2.2 `.ai()` 不创建 session、不进入工具循环

**决策：**

`.ai()` 只调用 `LLMService.chat_completion()`。`system` 可为空；`schema` 可为空；`max_tokens` 可为空。
`response_format="json"` 只转换成系统提示约束，不传 provider-native response format。

**原因：**

PR-AF gate、merge、polish 等步骤需要低成本单次调用，不应复用审计 session 状态。

**允许：**

```text
schema JSON 解析成功后 model_validate
无 schema 返回原始文本
response_cost_usd 为 None
```

**禁止：**

```text
创建 AuditSession
注册或执行工具
调用 chat_completion_stream
修改 LiteLLM model boundary
```

**影响：**

成本与 token 数据只透传，不聚合、不估算、不落业务报表。

## 2.3 动态终结工具固定叫 `FinalizeReview`

**决策：**

`SchemaFinalizeReviewTool` 位于 `backend/app/tool_gateway/schema_finalize_review.py`，固定
`name = "FinalizeReview"`、`always_load = True`、`input_model = schema`。

**原因：**

QueryLoop 通过 `TERMINAL_TOOL_NAMES = {"FinalizeReview"}` 判定终结。改名会复制运行时终结逻辑。

**允许：**

```text
schema 决定任意字段、嵌套结构、空集合、可选行号
校验成功返回 final_payload/completion_mode/terminal_action
校验失败返回 finalization_rejected/validation_errors
```

**禁止：**

```text
硬编码 rule_id、旧 severity、category、source
强制行号或路径白名单
校验失败时触发终结
```

**影响：**

每个 `.harness()` 调用持有独立 schema 工具，避免不同 schema 串扰；旧快速审查工具不修改。

## 2.4 `run()` 只增加两个私有扩展点

**决策：**

`RuntimeBridge.run()` 新增 `_tool_registry` 和 `_payload_extractor` 可选参数。二者非空时优先使用；
否则保持原 `_build_tool_registry()` 和 `extract_final_payload()` 行为。

**原因：**

`.harness()` 需要替换动态工具与 schema-aware extractor，但不复制主循环。

**允许：**

```text
`.harness()` 传入私有扩展点
现有调用方不传参数
```

**禁止：**

```text
将扩展点暴露成新公共 API
让 `.harness()` 直接改写旧 FinalizeReviewTool
在扩展点外动态改 run() 行为
```

**影响：**

现有快速审查、chat session 和其它 Bridge 调用方零行为变化。

## 2.5 `.harness()` 默认 bare mode

**决策：**

`RuntimeSessionAdapter` 新增 `enable_skills=True` 和 `enable_memory=True`。`.harness()` 显式传 `False`。

**原因：**

PR-AF 流程拥有自己的阶段 prompt；CodeSage skill discovery 和 runtime memory 会污染输入并影响可复现性。

**允许：**

```text
session、turns、tool calls、final payload 继续持久化
`.ai()` 和 `.harness()` 可同时调用同一 Bridge
```

**禁止：**

```text
bare mode 注入 skill prompt、route message、skill discovery snapshot
bare mode 注入 runtime memory
更改现有快速审查默认注入行为
```

**影响：**

深审流程保留底层可观测性，但不继承快速审查的领域上下文。

## 2.6 通用 JSON parser 归属 `app.utils`

**决策：**

`AgentJsonParser` 移动到 `backend/app/utils/agent_json_parser.py`。`bridge.py` 与 `query_loop.py` 更新 import；
删除 `backend/app/execution_plane/harness/`，不保留兼容 alias。

**原因：**

该工具是通用响应修复器，不是 execution plane 的业务 harness。

**允许：**

```text
runtime 和后续 PR-AF 流程共同复用 AgentJsonParser
```

**禁止：**

```text
从旧路径 import
保留临时 execution_plane/harness 目录
为迁移添加 alias
```

**影响：**

只允许两个运行时调用方直接更新，避免兼容层膨胀。

---

# 3. 范围、所有权与交付结构

## 3.1 本阶段交付

```text
backend/app/execution_plane/models/runtime_ai.py
backend/app/execution_plane/models/__init__.py
backend/app/execution_plane/runtime/bridge.py
backend/app/execution_plane/runtime/adapters/session.py
backend/app/execution_plane/runtime/query_loop.py
backend/app/utils/agent_json_parser.py
backend/app/tool_gateway/schema_finalize_review.py
backend/tests/runtime/test_bridge_deep_runtime.py
```

## 3.2 所有权

| 内容 | Owner | 职责 |
|---|---|---|
| `AIResult` / `HarnessResult` | execution plane model boundary | 定义 runtime AI 返回形状与错误契约 |
| `RuntimeBridge.ai()` | execution plane runtime | 单次模型调用与 schema 校验 |
| `RuntimeBridge.harness()` | execution plane runtime | 多轮工具循环、bare mode、终结 payload |
| `SchemaFinalizeReviewTool` | tool gateway | 按调用方 schema 校验并生成终结 payload |
| `AgentJsonParser` | utils | JSON 修复与提取 |
| `FinalReviewPayload` / `FinalizeReviewTool` | 快速审查契约 | 保持不变 |

`RuntimeBridge` 创建 session 与结果；`SchemaFinalizeReviewTool` 解释 schema 验证；业务 schema owner 解释
业务字段语义。报告和前端禁止直接依赖 `AIResult`，必须等待后续接入层。

## 3.3 目录 / 文件布局

```text
backend/app/
├── execution_plane/
│   ├── models/runtime_ai.py
│   └── runtime/bridge.py
├── tool_gateway/schema_finalize_review.py
└── utils/agent_json_parser.py
```

规则：

* 不创建 `execution_plane/harness`。
* 不创建 `CodeSagePrAfApp` 或 PR-AF 命名模块。
* 不把 JSON parser、结果模型和工具混入一个文件。

---

# 4. 共享约定

## 4.1 结果序列化

```text
python object = pydantic model 或原始文本包装
JSON output   = parsed.model_dump(mode="json", exclude_none=True)
ai cost       = response.response_cost_usd，缺失为 null
ai usage      = provider-returned normalized usage dict，缺失为 null
harness cost  = 不聚合，恒为 null
harness usage = 观测聚合 {"total_tokens": provider_tokens_used}，缺失为 null
```

Bridge 不估算 cost，不把 missing usage 重写为 0。

## 4.2 工具命名

动态和旧快速审查终结工具都叫 `FinalizeReview`。同一 registry 只允许存在一个；
`.harness()` 必须先过滤基础工具中的同名旧工具，再注册动态工具。

---

# 5. 核心模型

## 5.1 `AIResult`

**职责：**

包装一次单轮 LiteLLM 调用结果；不承载审查业务语义。

**字段：**

```text
parsed: BaseModel | None
text: str
content: str
raw_text: str
usage: dict[str, Any] | None
cost_usd: float | None
```

**不变量：**

```text
有 schema 时 parsed 必须是 schema 实例
无 schema 时 parsed 为 None
text == content == raw_text
属性代理不会覆盖 AIResult 自身字段或方法
```

**禁止承载：**

```text
session_id
tool calls
findings
```

## 5.2 `HarnessResult`

**职责：**

包装一次 schema 化多轮 harness 成功结果。

**字段：**

```text
parsed: BaseModel
session_id: str
result: dict[str, Any]
usage: dict[str, Any] | None
cost_usd: float | None
```

**不变量：**

```text
parsed 必须通过传入 schema 校验
result.session_id == session_id
result.final_payload 是 schema 允许的 JSON 表示
```

**禁止承载：**

```text
伪造成本
隐藏未终结失败
```

## 5.3 错误模型

```text
AIParseError: JSON 不能解析；raw_text 保留原始响应。
AISchemaValidationError: JSON 可解析但 schema 失败；validation_errors 保留 pydantic errors。
HarnessIncompleteError: 多轮循环和有界 retry 后仍无有效 payload。
```

错误必须显式抛出，不得用空 findings 或默认业务对象伪装成功。

---

# 6. 状态机

## 6.1 `.harness()` 终结状态

状态：

```text
RUNNING
FINALIZE_TOOL_ACCEPTED
ASSISTANT_JSON_ACCEPTED
FINALIZE_REJECTED
INCOMPLETE
```

合法转换：

```text
RUNNING → FINALIZE_TOOL_ACCEPTED
RUNNING → ASSISTANT_JSON_ACCEPTED
RUNNING → FINALIZE_REJECTED → RUNNING
FINALIZE_REJECTED → FINALIZE_TOOL_ACCEPTED
FINALIZE_REJECTED → ASSISTANT_JSON_ACCEPTED
FINALIZE_REJECTED → INCOMPLETE
RUNNING → INCOMPLETE
```

特殊规则：

* `FINALIZE_TOOL_ACCEPTED` 由 QueryLoop 的既有 terminal action 机制触发。
* `FINALIZE_REJECTED` 只作为工具反馈，不产生 `final_payload`。
* `ASSISTANT_JSON_ACCEPTED` 只在自然输出能通过 schema 校验时成立。
* `INCOMPLETE` 必须抛 `HarnessIncompleteError`。

---

# 7. 方法契约

## 7.1 `.ai()`

```python
async def ai(
    self,
    prompt: str,
    *,
    system: str | None = None,
    schema: type[BaseModel] | None = None,
    response_format: str | None = None,
    max_tokens: int | None = None,
) -> AIResult
```

行为：

1. 构造 system/user 消息。
2. 有 schema 时注入 JSON Schema，并要求只输出 JSON 对象。
3. 无 schema 时返回原始文本。
4. 有 schema 时 `AgentJsonParser.parse_any()` 解析，然后 `schema.model_validate()` 校验。
5. usage、cost 只透传；多轮 harness 的 usage 只暴露 QueryLoop 观测聚合 token 数，cost 不聚合。

错误：

```text
空/不可解析 JSON + schema → AIParseError
JSON 可解析但校验失败 → AISchemaValidationError
```

## 7.2 `.harness()`

```python
async def harness(
    self,
    prompt: str,
    *,
    schema: type[BaseModel],
    cwd: str | None = None,
    system_prompt: str | None = None,
    max_turns: int | None = None,
    tool_allowlist: set[str] | None = None,
    tools: list[Any] | None = None,
    event_sink: Callable[[dict[str, Any]], Any] | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
) -> HarnessResult
```

行为：

1. `schema` 必填；默认 `max_turns=50`。
2. 构造 bare mode session。
3. 构造独立 registry，基础工具来自 `tools or self._tools`。
4. 过滤普通工具 allowlist，但动态 `FinalizeReview` 始终保留。
5. `cwd` 写入 `recon_payload["deep_runtime_cwd"]` 或等价 advisory 字段，仅作为元数据。
6. 调用私有化 `run()`。
7. 校验 `final_payload` 后返回 `HarnessResult`。

registry 规则：

```text
base = list(tools) if tools is not None else list(self._tools)
base = [tool for tool in base if tool.name != "FinalizeReview"]
base.append(SchemaFinalizeReviewTool(schema))
```

`tool_allowlist` 非空时裁剪普通工具，`FinalizeReview` 不被裁剪。

## 7.3 Schema-aware extractor

Extractor 优先读取最近一次动态工具输出的：

```text
output_payload["final_payload"]
```

然后从最新 assistant 文本提取 JSON。任一路径必须通过 `schema.model_validate()`。
失败或不存在返回 `None`，交回有界 finalizer retry。

---

# 8. 错误矩阵

| 场景 | 行为 | 可观测结果 |
|---|---|---|
| `.ai()` 有 schema 且响应不可解析 | 抛 `AIParseError` | `raw_text` 保留 |
| `.ai()` JSON 可解析但 schema 失败 | 抛 `AISchemaValidationError` | `validation_errors` 非空 |
| `.ai()` 无 schema 且响应非 JSON | 成功返回文本 | 不抛解析错误 |
| 动态工具 payload 失败 | 返回 reject payload | 无 `final_payload` |
| assistant JSON schema 失败 | extractor 返回 None | 进入 finalizer retry |
| retry 后仍失败 | 抛 `HarnessIncompleteError` | session 保留 |
| LiteLLM 调用错误 | 沿用现有 runtime/model 错误路径 | 不映射成 INCOMPLETE 成功 |

---

# 9. 测试矩阵

| ID | 场景 | 断言 |
|---|---|---|
| D01 | `.ai()` schema 成功 | 直接访问 schema 字段、`model_dump()` 正确 |
| D02 | `.ai()` 无 schema | `.text` / `.content` 保留非 JSON |
| D03 | `.ai()` 解析失败 | `AIParseError.raw_text` 正确 |
| D04 | `.ai()` schema 失败 | `AISchemaValidationError.validation_errors` 非空 |
| D05 | parser 迁移 | `bridge` 与 `query_loop` 均从 `app.utils.agent_json_parser` import |
| D06 | 动态工具成功 | `final_payload`、`completion_mode`、`terminal_action` 正确 |
| D07 | schema 隔离 | 不含旧 `rule_id/category/source` 硬约束 |
| D08 | 动态工具拒绝 | `finalization_rejected=True`，无 `final_payload` |
| D09 | tool-call harness | `HarnessResult.parsed` 是 schema 实例 |
| D10 | assistant JSON fallback | 无工具调用也能成功 |
| D11 | invalid 后重试 | 第一次不终止，第二次成功 |
| D12 | 最终失败 | 抛 `HarnessIncompleteError` |
| D13 | 并发 schema 隔离 | 两个 schema 结果互不串扰 |
| D14 | bare mode | skill/memory 不注入，session/tool calls 保留 |

---

# 10. 边界条件

* `cwd` 与工具 root 不同：只产生 advisory metadata，不能扩大读取权限。
* `tools=[]`：允许 assistant JSON 终结，不得崩溃。
* `max_turns=1`：主循环允许一次模型调用；未终结进入有界 finalizer retry。
* 空 findings、空 sub_reviews、空 `file_path`、`line_start=0`：schema 允许则接受。
* schema `extra="forbid"`：多余字段必须拒绝。
* PR-AF severity alias：由 PR-AF schema validator 处理，Bridge 不重复实现。
* 旧快速审查 reject payload：保持现有字段与行为。

---

# 11. 验收

必须全部满足：

1. `pytest backend/tests/runtime/test_bridge_deep_runtime.py` 通过。
2. `pytest backend/tests/runtime/test_bridge.py` 通过。
3. `pytest backend/tests/pr_review/test_finalize_review.py` 通过。
4. `pytest backend/tests/tooling` 通过。
5. `pytest backend/tests/runtime` 通过。
6. 源码中不存在 `app.execution_plane.harness` import 或目录。
7. 快速审查契约文件和旧 FinalizeReviewTool 的 git diff 为空。

---

# 12. 延后工作

* PR-AF orchestrator 与阶段 schema 迁移。
* CLI `--repo --base --head` 接口。
* AACR 数据集加载、过滤与评测。
* 前端报告接入。
* 多调用 cost/usage 聚合。
