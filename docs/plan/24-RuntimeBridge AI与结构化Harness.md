# Plan 24：RuntimeBridge `.ai()` 与 Schema Harness 终结能力

版本：**v2**。状态：**已批准**。源码基线：`a4595a82d60f`。

---

## 1. 目标

本阶段只完成两个 RuntimeBridge 基础能力：

1. 实现 PR-AF 兼容的 `.ai()`：单次 LiteLLM 调用 + JSON 解析 + pydantic schema 校验。
2. 实现 PR-AF 兼容的 `.harness()`：多轮工具循环 + 按 schema 动态生成的 `FinalizeReview` 终结工具。

不迁移 PR-AF orchestrator，不改 CLI，不接 AACR，不接前端报告，不新增 `CodeSagePrAfApp`。

---

## 2. 归属修正

### 2.1 结果模型归属

不再把 `AIResult` / `HarnessResult` 放到独立 harness 目录。

统一放入：

```text
backend/app/execution_plane/models/runtime_ai.py
```

该文件承载：

- `AIResult`
- `HarnessResult`
- `AIParseError`
- `AISchemaValidationError`
- `HarnessIncompleteError`

并在 `backend/app/execution_plane/models/__init__.py` 导出。

### 2.2 JSON 解析器归属

现有 `backend/app/execution_plane/harness/json_parser.py` 是通用 JSON 修复/提取工具，移动到：

```text
backend/app/utils/agent_json_parser.py
```

同步更新 `bridge.py` 与 `query_loop.py` 调用方，然后删除
`backend/app/execution_plane/harness/`，不保留兼容 alias。

### 2.3 工具归属

动态终结工具放入：

```text
backend/app/tool_gateway/schema_finalize_review.py
```

命名 `SchemaFinalizeReviewTool`，固定实现 `name = "FinalizeReview"`，以命中现有 QueryLoop 终结逻辑。

---

## 3. 现有契约与影响边界

| 位置 | 现状 | 本计划动作 |
|---|---|---|
| `backend/app/execution_plane/runtime/bridge.py` | `RuntimeBridge` 无 `.ai()` / `.harness()` | 新增两个方法 |
| `backend/app/execution_plane/runtime/bridge.py` | `run()` 固定构建 registry，固定使用旧 payload extractor | 增加私有可选扩展点 |
| `backend/app/tool_gateway/finalize_review.py` | 旧快速审查 `FinalizeReviewTool` | 不修改 |
| `backend/app/contracts/final_review_contract.py` | 旧快速审查 schema | 不修改 |
| `backend/app/execution_plane/runtime/query_loop.py` | `FinalizeReview` 是终结工具名 | 动态工具必须同名 |
| `backend/app/execution_plane/runtime/query_loop.py` | 从 `output_payload.final_payload` 提取结果 | 动态工具输出必须保持该形状 |
| `E:\Mac\github\pr-af\src\pr_af\reasoners\harnesses.py` | `.ai()` 后直接读 schema 字段 | `.ai()` 返回可代理 schema 字段的对象 |
| `E:\Mac\github\pr-af\src\pr_af\merge_gate.py` | 无 schema `.ai()` 后读 `.text` / `.content` | `.ai()` 无 schema 时返回原始文本包装对象 |
| `E:\Mac\github\pr-af\src\pr_af\reasoners\harnesses.py` | `.harness()` 返回 `.parsed` | `.harness()` 返回 `HarnessResult` |

---

## 4. 实现设计

### 4.1 `.ai()`

新增 `RuntimeBridge.ai()`。该方法直接调用 `LLMService.chat_completion()`，不创建 session，不进入工具循环。

有 schema 时注入 `schema.model_json_schema()`，使用迁移后的 `AgentJsonParser` 解析 JSON，并使用
`schema.model_validate()` 校验。返回的 `AIResult` 属性代理到 `parsed`。

无 schema 时不强行解析 JSON，返回原始 `text` / `content`。`response_format="json"` 只作为提示约束处理，
本阶段不改 LiteLLM model boundary。JSON 解析失败抛 `AIParseError`；schema 校验失败抛
`AISchemaValidationError`。不伪造 cost：有 `response_cost_usd` 就透传，否则为 `None`。

### 4.2 `SchemaFinalizeReviewTool`

动态终结工具完全由传入 pydantic schema 生成 `input_model` 和 `input_schema`，不硬编码 CodeSage 快速审查字段，
也不硬编码 PR-AF finding 字段。校验成功返回：

```python
{
    "final_payload": parsed.model_dump(mode="json", exclude_none=True),
    "completion_mode": "finalize_tool",
    "terminal_action": "finalize_review",
}
```

校验失败返回：

```python
{
    "finalization_rejected": True,
    "validation_errors": [...],
}
```

不触发终结。不新增业务校验；是否允许空 findings、空 sub_reviews、空 `file_path`、`line_start=0`
或 extra fields 由传入 schema 决定。

### 4.3 `.harness()`

新增 `RuntimeBridge.harness()`。默认 `max_turns=50`；每次调用构造独立 registry。基础工具来自参数 `tools`
或 `self._tools`，过滤已有 `FinalizeReview`，追加本次 schema 专属动态终结工具。`tool_allowlist` 只裁剪普通工具；
`FinalizeReview` 始终保留。

`cwd` 只写入 `recon_payload`，作为 advisory metadata。它不动态扩大文件工具权限，不临时挂 Shell；
文件访问边界仍由构造 `RuntimeBridge` 时传入的工具决定。

使用 schema-aware extractor，支持动态 `FinalizeReview` 和 assistant JSON fallback 两条成功路径。
最终仍无有效 payload 时抛 `HarnessIncompleteError`。返回：

```python
@dataclass
class HarnessResult:
    parsed: BaseModel
    session_id: str
    result: dict[str, Any]
    usage: dict[str, Any] | None
    cost_usd: float | None
```

### 4.4 `.harness()` 纯净模式

为避免快速审查的 skill/memory 注入污染 PR-AF prompt，`.harness()` 默认使用 bare runtime mode：
禁用 skill discovery 注入和 runtime memory 注入；保留 session、turns、tool calls、final payload 的持久化。

通过 `RuntimeSessionAdapter` 增加可选参数实现：

```python
enable_skills: bool = True
enable_memory: bool = True
```

默认值保持 `True`，现有快速审查行为不变。

---

## 5. `run()` 最小扩展

为避免复制 RuntimeBridge 主循环，`run()` 增加两个私有扩展点：

```python
_tool_registry: ToolRegistry | None = None
_payload_extractor: Callable[[Any], dict[str, Any] | None] | None = None
```

内部逻辑改为：

```python
tool_registry = _tool_registry or self._build_tool_registry(tool_allowlist=tool_allowlist)
payload_extractor = _payload_extractor or self.extract_final_payload
```

只有 `.harness()` 使用这两个参数。现有调用方不传参数，行为完全不变。

---

## 6. 预计代码变更

### 6.1 新增

| 文件 | 内容 |
|---|---|
| `backend/app/execution_plane/models/runtime_ai.py` | `AIResult`、`HarnessResult`、AI/Harness 错误类型 |
| `backend/app/utils/agent_json_parser.py` | 从旧 harness 目录迁移的 `AgentJsonParser` |
| `backend/app/tool_gateway/schema_finalize_review.py` | `SchemaFinalizeReviewTool` |
| `backend/tests/runtime/test_bridge_deep_runtime.py` | `.ai()`、`.harness()`、动态 finalizer 测试 |

### 6.2 修改

| 文件 | 变更 |
|---|---|
| `backend/app/execution_plane/models/__init__.py` | 导出新契约 |
| `backend/app/execution_plane/runtime/bridge.py` | 新增 `.ai()`、`.harness()`；更新 JSON parser import；扩展 `run()` 私有扩展点 |
| `backend/app/execution_plane/runtime/adapters/session.py` | 新增 `enable_skills` / `enable_memory` |
| `backend/app/execution_plane/runtime/query_loop.py` | 更新 JSON parser import |

### 6.3 删除

```text
backend/app/execution_plane/harness/
```

### 6.4 禁止修改

| 内容 | 原因 |
|---|---|
| `backend/app/contracts/final_review_contract.py` | 快速审查契约不变 |
| `backend/app/tool_gateway/finalize_review.py` | 快速审查终结工具不变 |
| `backend/app/domains/pr_review/orchestrator.py` | 三视角快速审查不在本阶段 |
| LiteLLM model service/client | 不引入 provider-native structured output |
| 前端、报告、CLI、AACR | 延后 |

---

## 7. 测试与验收

### 7.1 功能测试

1. `.ai()` schema 成功：返回对象可直接访问 schema 字段，`model_dump()` 正确。
2. `.ai()` 无 schema 成功：普通文本原样返回，不因非 JSON 抛错。
3. `.ai()` JSON 解析失败：抛 `AIParseError`，错误对象保留 raw text。
4. `.ai()` schema 校验失败：抛 `AISchemaValidationError`，包含 pydantic validation errors。
5. `AgentJsonParser` 迁移后：`bridge.py` 与 `query_loop.py` import 正确。
6. 动态 `FinalizeReview` 成功：输出 `final_payload` 和 `terminal_action="finalize_review"`。
7. 动态 `FinalizeReview` schema 与旧 schema 隔离：不出现 `rule_id/category/source` 硬约束。
8. 动态 `FinalizeReview` 失败：返回 `finalization_rejected=True`，不产出 `final_payload`。
9. `.harness()` tool-call 终结：`HarnessResult.parsed` 是 schema 实例。
10. `.harness()` assistant JSON fallback：无需工具调用也能 schema 校验成功。
11. `.harness()` invalid tool call 后重试：第一次不终止，第二次成功。
12. `.harness()` 最终失败：抛 `HarnessIncompleteError`，不伪装成功。
13. 并发 schema 隔离：两个 `.harness()` 使用不同 schema，互不串扰。
14. bare mode：skill/memory 不注入，session/tool calls 仍持久化。

### 7.2 回归测试

```bash
pytest backend/tests/runtime/test_bridge_deep_runtime.py
pytest backend/tests/runtime/test_bridge.py
pytest backend/tests/pr_review/test_finalize_review.py
pytest backend/tests/tooling
pytest backend/tests/runtime
```

### 7.3 边界验收

- `cwd` 存在但文件工具 root 不同：只能得到 advisory metadata，不能读取未授权路径。
- `tools=[]`：`.harness()` 不崩溃，仍可通过 assistant JSON 终结。
- `max_turns=1`：允许一次模型调用；未终结则进入有界 finalizer retry。
- schema 含空 findings、空 sub_reviews、空 `file_path`、`line_start=0`：只要 schema 允许，Bridge 必须接受。
- schema 禁止 extra fields 时，多余字段必须被 pydantic 拒绝。
- PR-AF severity alias 由 PR-AF schema validator 处理；Bridge 不重复实现。
- 旧快速审查 `FinalizeReview` 的 schema、校验、reject 行为完全不变。
