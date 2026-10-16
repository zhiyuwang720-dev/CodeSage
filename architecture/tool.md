目前 DeepReview 的机制可以概括为：**模型通过 Function Call 提出工具请求，Runtime 负责对话循环，ToolGateway 校验并执行本地工具，再把结果送回模型；模型最终通过 `FinalizeReview` 提交当前阶段的结构化结果。**

Function Call 是模型与程序之间的调用协议，Tool 则是我们在本地实现的具体能力。模型拿到的是工具名称、说明和参数 Schema，真正的 Git 读取、路径检查、超时控制和结果保存都由程序完成。

下面按照当前代码的实际调用链展开。

### 1. 哪些阶段会调用工具

| 阶段 | 调用方式 | 工具使用 | 最终输出 |
|---|---|---|---|
| Semantic | `.ai()` | 不提供工具 | 从响应文本解析并校验 JSON |
| Planner | `.harness()` | 四个只读工具＋终结工具 | `ReviewPlanDraft` |
| 每个 Reviewer | 独立 `.harness()` | 四个只读工具＋终结工具 | `ReviewerResultDraft` |
| Cross Analysis | `.harness()` | 四个只读工具＋终结工具 | `CrossAnalysisResultDraft` |

Semantic 的 Schema 当前是附在提示词里的，随后由本地解析、Pydantic 校验；它没有工具循环，也没有调用 `FinalizeReview`。

另外三个阶段进入共同的 Harness 执行机制，但分别使用自己的提示词、输出模型和会话。

入口实现见 [RuntimeBridge](E:/Mac/CodeSage/backend/app/execution_plane/runtime/bridge.py:815)。

### 2. 开始 Harness 时，工具如何准备

领域层先创建四个只读工具，并把本次审查的仓库位置、固定 head、已解析 diff 和配置注入其中。

| 工具 | 参数概貌 | 实际读取来源 |
|---|---|---|
| `file_read` | 文件路径、起止行号 | `git show <head>:<path>` |
| `file_read_diff` | 文件路径、diff 起始行、读取行数 | 内存中的 `snapshot.diff_by_path` |
| `file_find` | 文件名或路径关键字 | 固定 head 的 Git 文件树 |
| `code_search` | 查询词、是否正则、路径前缀 | 固定 head 上的 `git grep` |

这里有两个重要区别。

第一，**读取 diff 使用已经解析好的内存数据；读取完整源码使用固定 Git 提交。** 它们都不会把当前工作区内容当成审查事实。

第二，工具读取范围与 Reviewer 的 Finding 提交范围不同。Reviewer 可以读取未变更的调用方、消费者等上下文文件；`file_read_diff` 则要求路径属于允许的变更范围。最终 Finding 能否落在当前维度的目标文件上，要由后面的领域校验处理。

工具的创建位置是 [catalog.py](E:/Mac/CodeSage/backend/app/domains/deep_review/tools/catalog.py)，路径规则在 [DeepToolContext](E:/Mac/CodeSage/backend/app/domains/deep_review/tools/base.py:22)。

`.harness(schema=...)` 接着根据这个阶段的输出模型创建：

```python
SchemaFinalizeReviewTool(schema)
```

因此，同名的 `FinalizeReview` 在不同会话中有不同的参数结构：

```text
Planner 会话 → ReviewPlanDraft
Reviewer 会话 → ReviewerResultDraft
Cross 会话 → CrossAnalysisResultDraft
```

它与四个读取工具一起放入本次调用独立的 `ToolRegistry`。传入的工具会先经过 allowlist 过滤，已有的同名终结工具也会被替换，确保使用当前 Schema。

这条路径使用 DeepReview 自己的注册表，并选择通用 Harness 工具提醒；旧审查工具和旧工具提醒不会自动被带入。相关逻辑见 [harness()](E:/Mac/CodeSage/backend/app/execution_plane/runtime/bridge.py:891)。

### 3. 本地工具如何变成模型看到的 Function Schema

每个工具通过 `RuntimeTool` 定义三项核心信息：

```text
name         工具名称
description  工具用途说明
input_model  Pydantic 参数模型
```

注册表调用工具的 `describe()`，通过：

```python
input_model.model_json_schema()
```

生成参数 Schema。模型客户端再把它转换为 Function Calling 格式，例如：

```json
{
  "type": "function",
  "function": {
    "name": "file_read",
    "description": "Read a bounded line range from the fixed head commit.",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1}
      },
      "required": ["path"]
    }
  }
}
```

上面是简化示意，实际 Schema 直接来自工具的 Pydantic 模型。

这意味着参数定义只有一个主要来源：**同一个模型既生成发送给 LLM 的 Schema，也负责执行前的本地校验。**

注册与 Schema 描述分别在 [ToolRegistry](E:/Mac/CodeSage/backend/app/tool_gateway/runtime.py:190) 和 [RuntimeTool](E:/Mac/CodeSage/backend/app/contracts/tools.py:45)。

### 4. 一轮 Function Call 如何完成

整体执行关系如下：

![img.png](./assets/img_tool.png)

每轮开始时，`QueryLoop` 加载会话历史和运行状态，整理上下文，取得当前工具定义，然后请求模型。请求经过：

```text
RuntimeLLMModelClient
  → LLMService
  → 统一模型客户端
  → LiteLLM
  → 配置的模型端点
```

调查阶段允许模型返回多个工具调用，没有强制它必须选择某一个工具。

流式响应中的工具参数会分片到达。统一模型客户端先累积参数，**只有参数形成完整 JSON，才交付可执行工具请求**；截断的半份 JSON 不会进入 ToolGateway。

Runtime 将模型响应归一化为：

```text
ToolCallRequest
  id     调用关联 ID
  name   工具名称
  input  参数对象
```

然后交给 ToolGateway。流式参数处理见 [模型客户端](E:/Mac/CodeSage/backend/app/execution_plane/models/client.py:785)，对话循环见 [QueryLoop](E:/Mac/CodeSage/backend/app/execution_plane/runtime/query_loop.py:132)。

### 5. ToolGateway 负责什么

ToolGateway 在执行一个请求前依次完成：

1. 根据名称找到已注册工具。
2. 用工具的参数模型解析和校验输入。
3. 判断并发安全性和执行顺序。
4. 创建调用记录，检查运行时权限与工具权限。
5. 在超时限制内执行工具。
6. 保存结果、错误信息、耗时及相关事件。

普通工具返回的核心对象是：

```text
ToolExecutionPayload
  content         供模型阅读的结果
  output_payload  供程序和审计使用的结构化数据
  is_error        是否执行失败
  metadata        附加信息
```

未知工具、参数错误、路径错误或超时通常会转成可反馈的工具结果。模型随后可以修改参数再调用，而不是让一次错误立即终止整个 Harness。

例如，把未变更文件交给 `file_read_diff` 时，返回信息会提示改用 `file_read`。参数格式合法和路径可读取是两层检查，不能仅依赖 Function Schema。

执行实现见 [ToolGateway](E:/Mac/CodeSage/backend/app/tool_gateway/runtime.py:397)。

这里存在两种并行：

- 多个 Reviewer 分别运行自己的模型会话，由共享 semaphore 控制。
- 一个会话内，模型一次提出多个只读工具调用，ToolGateway 可以并行执行。

四个读取工具声明为并发安全；`FinalizeReview` 声明为非并发安全，构成顺序边界。

### 6. 工具结果如何回到模型

工具结果不会只写日志。QueryLoop 将请求和结果追加到会话历史，记录调用 ID、工具名称、参数、输出和状态，下一轮模型请求会带上这些内容。

当前实现有专门的消息编码层。使用原生工具消息格式时，历史会恢复为类似：

```text
assistant.tool_calls:
  id = call_123
  function = file_read
  arguments = ...

tool:
  tool_call_id = call_123
  content = 文件内容或错误反馈
```

调用与返回通过 ID 配对。实现也支持兼容消息格式，最终格式由端点协议及配置决定。**因此不能笼统地说当前所有工具历史都作为普通 user 文本发送。**

这部分在 [工具消息编码器](E:/Mac/CodeSage/backend/app/tool_gateway/codec.py:20)。

模型如果只在自然语言或代码块里“写出一个工具调用”，当前 QueryLoop 默认不会执行它；`ALLOW_LEGACY_TEXT_TOOL_CALLS=False`，执行路径以原生调用事件为准。

### 7. `FinalizeReview` 为什么能结束会话

`FinalizeReview` 是同一套工具机制中的特殊工具，它接收的参数就是当前阶段的结果模型。

收到调用后，本地执行：

```python
schema.model_validate(raw_input)
```

校验失败时返回字段错误及 `finalization_rejected=true`，让模型修正。它不会返回 `final_payload`，因此不会被视为完成。

校验成功时返回：

```json
{
  "final_payload": {},
  "completion_mode": "finalize_tool",
  "terminal_action": "finalize_review"
}
```

QueryLoop 识别到有效 `final_payload` 后停止后续模型轮次。Bridge 还会再次用阶段 Schema 校验，随后返回结果与 session、usage 信息。

动态终结工具见 [SchemaFinalizeReviewTool](E:/Mac/CodeSage/backend/app/tool_gateway/schema_finalize_review.py:33)。

**结构校验通过，只代表阶段输出符合模型要求。** Planner 后面还有 repair；Reviewer 后面还有目标路径、行号及候选映射检查；Cross 后面还有候选索引、裁决和新增 Finding 的整理。Function Calling 本身不会证明评论是真的。

### 8. 模型迟迟不终结时怎么办

主调查循环有轮次上限。如果结束时还没有有效结果，Bridge 会进入一个额外的、有界终结阶段。

当前按端点能力选择策略：

| 能力配置 | 终结请求行为 |
|---|---|
| 支持 thinking 与强制工具调用 | 保留原消息前缀和工具集合，强制选择 `FinalizeReview` |
| 必须关闭 thinking 才能强制调用 | 关闭 thinking，只提供终结工具，使用独立终结系统提示并整理历史证据 |

第二条兼容路径会改变消息前缀，可能影响缓存复用；
第一条通过追加指令尽量保留原前缀。

强制终结阶段最多允许两次模型请求，第二次可以用于修正第一次的 Schema 错误。目标工具缺失时本地报错；模型返回其他工具时也会被检查拦截。HTTP 400、认证错误及终结请求预算耗尽属于停止重试的情况。

终结策略见 [Bridge 的结果收尾](E:/Mac/CodeSage/backend/app/execution_plane/runtime/bridge.py:1470)。
