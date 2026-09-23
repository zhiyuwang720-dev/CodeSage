# Deep Review 25.4：Semantic / Planner 真实数据验收记录

日期：2026-09-23。范围止于 Plan，不执行并行 Reviewer、Cross Analysis 或 Judge。

## 结论

本轮用 AACR Bench 的 3 个真实仓库用例调用了已配置的真实 LLM。Linera 和 mpv 完整通过 Input → Filter → Anatomy → Semantic → Planner → Plan Repair：11 条参考评论所在的路径全部属于主动审计维度，且每个待审文件恰有一个主维度。Ollama 的 22 个待审文件在 16 轮内未完成 Plan；因此不能声称大 PR 的规划可靠，也不能用路径覆盖率冒充 Finding Recall。

这轮没有执行 Reviewer，所以没有 Finding、Precision 或真正的 Recall 数值。参考评论本身也有可疑主张，不能把“预测全部评论”当作 Planner 的目标。

## 改动与边界

- Semantic 的系统提示词改为先做意图、实现、风险边界和证据不确定性的判断，再填写简单 Schema；用户证据移入独立模板。固定 patch 预算按文件公平分配，避免靠后的文件完全没有差异片段。
- Planner 的系统提示词明确“划分调查工作和覆盖边界，不预报 Finding 数量”；用户模板分开传递可信运行约束与不可信的 PR / Semantic / Anatomy 线索。配置中的维度数、文件数和轮次上限都显式传给模型，并要求为终结工具预留轮次。
- `file_read_diff` 对未修改、被排除或越界路径仍拒绝读取，但现在返回可恢复的 `invalid_path` 工具结果；不会因一次模型选错工具而中断 Harness。
- 结构化 `.ai` 响应若没有 JSON 对象，按解析失败处理，而不是误报 Schema 校验失败。Harness 无法完成时，异常携带已创建的 session ID 和已记录的 usage，供 `plan_failed` 事件保留观测数据。
- 没有改旧 Quick Review 的提示词或审计链路，也没有把 benchmark、密钥或实际调用记录写入产品数据库。

## 方法和数据边界

数据集是 `aacr-bench-main/evaluation/data/aacr_bench.jsonl`；仓库使用已有的 `aacr-bench-main/evaluation/repo/` 缓存。运行通过 `--allow-model-calls` 显式开启；模型配置取 `backend/.env`，本文不记录密钥或供应商响应正文。Runtime transcript 存在每次运行自己的 SQLite 文件，Deep Review 事件与报告在被 Git 忽略的 `.codesage/deep-review-smoke-25.4-20260923d/`；Ollama 修复后的第二次尝试位于后缀 `20260923e` 目录。`cost_usd` 均未由供应商适配器提供，不能据此断言零成本。

输入构建按 **merge-base → head** 取得 PR 变更，而不是对数据集的 base/head 直接做 `git diff base head`。因此 Linera 的有效变更是 4 个文件、mpv 是 5 个、Ollama 是 23 个；目录过滤分别排除 0、0、1 个，最后待审文件是 4、5、22 个。数据集没有 PR 标题/描述，Semantic 只能从提交信息与代码推断声明意图；这也是三个用例置信度不能过高的重要原因。

| 用例 | Semantic | Planner / Repair | 待审文件 | 主动维度 | 参考评论路径 |
| --- | --- | --- | ---: | ---: | ---: |
| `linera-io__linera-protocol@024925d` | 模型输出，置信度 0.55；4,703 tokens | 成功，实际 12 轮；34,172 tokens | 4 | 3，无延期 | 4/4 主动覆盖 |
| `mpv-player__mpv@dbd327d` | 模型输出，置信度 0.55；7,172 tokens | 成功，实际 12 轮（上限 16）；37,695 tokens | 5 | 4，无延期 | 7/7 主动覆盖 |
| `ollama__ollama@b6002f6` | 两次均为模型输出 | 未完成；首轮被工具路径校验中断，修复后在 16 轮上限仍持续搜索，未调用终结工具 | 22 | 无可用 Plan | 不可计算 |

上述 token 是成功阶段的会话/适配器记录，不是总账。失败的 Ollama 首轮 Planner 会话另外记录 18,790 tokens，第二轮 Planner 会话记录 44,078 tokens；两轮各自的 Semantic 调用也产生用量。失败阶段原来的 `plan_failed` 事件没有 session/usage，因而事件汇总曾低估消耗；本轮已修补异常观测契约，但尚未用新的真实失败再次验证。

## 逐阶段质量观察

Linera：Semantic 识别了 grace period 参数化、默认值从 0.2 到 0.1 的行为变化、调用方传 `None`，并明确较长 diff 被截断。Planner 后续用 `file_read_diff`/`file_read` 查到了完整函数，将 quorum 循环、调用方消费结果、注释及错误字符串分成 3 个调查维度。参考评论里的负数/NaN `mul_f64` 风险和第二个拼写错误都被明确点名；`impl Into<Option<f64>>` 的接口契约被纳入调查，但没有直接预判其风格结论。关于 `'vote_wait` 标签“没有被引用”的参考评论与计划所见代码中的 `break 'vote_wait` 冲突，不能机械地视为漏报。

mpv：Semantic 辨识了全局带锁 RNG 转为调用方持有状态、shuffle 改用整数区间随机数、临时文件名生成的种子生命周期变化。Planner 将 RNG 核心及范围映射、playlist 边界、临时文件重试、全局初始化删除分为 4 个维度。参考评论中的整数窄化、并发/种子碰撞关切、`mp_round_next_power_of_2` 溢出边界都有明确调查入口；命名措辞、文档链接和 typedef 惯例并未作为单独调查目标。这符合 Plan 不提前列缺陷的职责，但也表明“路径 7/7”不等于“关注点 7/7”。参考评论称大于 `2^31` 的范围会导致极低接受率，这个推论本身也需要 Reviewer 验证，而不能直接当真。

Ollama：首次 Planner 在读取未变更关联文件的 diff 时失败，工具安全拒绝是正确的，但错误不应使整个运行失败。改为可恢复工具结果后，第二次通过了该点，却在 16 轮内持续作跨文件搜索，随后终结阶段仍尝试搜索工具，导致 `FinalizeReview` 没有发生。修订后的提示词已强调抽样检查、将未验证关系留给 Reviewer、并给最终交付预留两轮；这一文本约束尚未在真实 Ollama 运行中验证，不能宣称已解决大 PR 收尾。

## 风险和建议的下一步

1. **大 PR 的 Planner 终结仍需真实验收。** 已追加下述强制终结实现；在模型可以正常响应时，需要复测它能否在 16 轮后生成有效 Plan。不要靠无限增加调查轮数解决。
2. **Plan 覆盖不能代替审计效果。** 下一阶段至少让这两个成功 Plan 进入 Reviewer 和 Cross Analysis，再按有效、可复现的参考评论评估 Finding Recall；噪声评论应单独标注。
3. **成本观测要覆盖失败。** 已增加异常携带 session/usage 的代码与单元测试；后续真实限流失败验证了 session ID 和零用量会写入 `plan_failed`，但尚未验证“已有付费调用后再失败”的路径。

本轮的完整成功产物在上述 `.codesage` 目录的 `report.json`、`summary.json` 和 `events/`；失败运行保留了 JSONL 事件及 SQLite transcript，可复核工具调用和停机原因。真实 API 测试保持 opt-in，不进入普通 CI。

## 2026-09-23 补充：有界强制终结

Deep Review Harness 的正常调查仍使用原有角色轮数。调查结束且没有已接纳结果时，Runtime 只提供动态 `FinalizeReview` 工具，并在实际流式模型请求上设置强制 `tool_choice`、关闭并行工具调用。最终化最多发出两次模型请求：第一次 Schema 校验失败，可以带着校验错误再提交一次；仍失败则保留 session 和 usage，返回 `HarnessIncompleteError`。普通旧审查的终结行为未改变。缺少强制工具选择能力的供应商在付费调用前被拒绝。

本地验证覆盖：正常调查输出 JSON 后仍进入终结工具、无效载荷的一次纠错、两次请求上限、工具选择从服务层传到 SDK，以及不支持强制选择时提前拒绝。最初两次 16 轮 Ollama 复测产物分别在 `.codesage/deep-review-smoke-25.4-forced-finalizer-20260923/` 和同名 `20260923b/`；两次均在 Semantic 阶段遇到 `ModelRateLimitError`，没有运行到强制终结。后续一次网络放行的复测及真实终结故障见下节；因此此处先前“没有真实模型终结证据”的结论已由新轨迹补充为“调查阶段真实完成，但终结请求不兼容”。

轮数策略尚未改动：两个已成功的小用例都实际使用了 12 轮，并在第 12 轮完成终结。因此“≤8 文件统一压到 8 轮”需要在强制终结可用时重新做质量对比，不能仅依据文件数认定它足够。

## 2026-09-23 补充：Ollama Harness 轨迹复测与评分

复测用例：`ollama__ollama@b6002f6`；调查上限 16 轮；目标仓库与数据集沿用本节前述 AACR 缓存。首次启动时默认 Python 缺少 `litellm`，随后在安装了项目依赖的 `langchain1.2` 环境中运行；沙箱网络连接失败。获准的网络执行中 Semantic 成功，Planner 完成调查但最终未产出可验证的 ReviewPlan。真实运行的事件、报告前状态和 Runtime transcript 分别保存在：

- `.codesage/deep-review-smoke-25.4-ollama-rerun-20260923-escalated/ollama__ollama@b6002f6/events/de1c2eb01b26/events.jsonl`
- `.codesage/deep-review-smoke-25.4-ollama-rerun-20260923-escalated/ollama__ollama@b6002f6/sessions.sqlite3`

本次事件链显示输入有 23 个变更文件、目录过滤排除 1 个、最终审查 22 个；Intake 用时约 0.96 秒。Blast Radius 用时约 0.08 秒，返回 0 个 related path。Anatomy 用时不足 1 毫秒，将 22 个文件分为 `kvcache`（3 个）、`ml`（2 个）、`model`（16 个）和 `runner`（1 个）四组。该分组与改动边界吻合；Blast Radius 的 0 个结果只能理解为此模块没有识别出关联路径，不能据此推断不存在跨文件影响。

Semantic 来自模型，confidence 为 0.62，耗时约 12.7 秒。记录到 11,474 tokens（输入 9,141、输出 2,333、其中 reasoning 1,156；cost 未提供）。它抓住了批次字段从切片转为 Tensor、多个模型调用点迁移、CPU tensor 访问路径、`Batch.Sequences` 仍为 `[]int`、Runner 构造证据不完整和没有测试文件等问题；风险假设覆盖 nil Tensor、接口实现者、view 连续性、切片别名和索引长度。整体是有用的导航摘要，但由于 Semantic confidence 只有 0.62，Planner 提示中要求把它当作未验证线索是必要的。

Planner 的调查阶段走满 16 轮，随后进入第 17 个终结 turn；Planning 总耗时约 72.9 秒。session 里有 50 次成功工具调用：`file_read_diff` 22 次、`file_read` 11 次、`code_search` 17 次。22 次 diff 读取覆盖 22 个不同路径，既无遗漏也无重复；11 次文件读取都成功且未截断。搜索中有 3 次无匹配、1 次结果截断；对 `sync` 和 `Bytes()` 的搜索存在相近主题的重复探索，但参数不相同，也没有证据表明模型被困在同一问题上。所有 50 个工具调用均为 `completed`，没有失败工具调用，也没有读写越界记录。

### Harness 轨迹判断

调查阶段没有发现相同工具及相同参数的重复调用。调用路径大体有序：先逐个读取全部 22 个 diff，再查 Tensor 接口、实现和使用点，然后检查测试与具体文件范围。平均每个调查 turn 约 3.1 次工具调用；第 5 轮有 8 次读取，说明大批并行读取确实会让单轮调用数升高，但每个路径各不相同。调查质量总体较好，主要效率损失来自 `sync` / `Bytes()` 的相近搜索反复，而不是原地重复执行同一工具。

终结阶段暴露出实质性兼容问题。session 共记录 17 个 turn、50 个工具调用、123 个 checkpoint；最后一个 turn 标为 `resumable_failed`。最后阶段的模型错误是 `Thinking mode does not support this tool_choice`：两次实际强制工具请求均被服务端以参数不兼容拒绝；之后 QueryLoop 又把“强制终结请求预算已用尽”当作可重试模型错误，生成了 4 个本地失败检查点。后四次在本地就被请求计数器拦截，没有发出额外供应商请求，但重复重试没有恢复机会。DeepSeek 官方 Chat Completions 文档说明思考模式默认启用，且思考模式不支持 `required` 或命名工具选择；要使用命名工具选择，必须先关闭思考模式。[DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)

因此本次没有接受 ReviewPlan，Plan 维度数、文件责任分配、参考评论路径覆盖率和 Finding Recall 均不可评分。session 中 Planner 的 `provider_tokens_used`、input/output token counters 均为 0，`plan_failed` 也记录 0 tokens；但同一 session 有 16 个模型产生的调查回合和 50 次工具调用。这个 session 不能用于估算 Planner 的实际 token 或费用，当前运行观测存在明显欠账。`cost_usd` 同样不可用。

### 评分

评分采用 10 分制；端到端分数把“生成并接纳有效 ReviewPlan”视为验收核心，因此终结失败会显著拉低总分。

| 维度 | 分数 | 依据 |
| --- | ---: | --- |
| Intake、过滤与 Anatomy | 9/10 | 22 个目标文件和 4 个模块分组清楚，运行快速；Blast Radius 的零结果仍须谨慎解释。 |
| Semantic 导航质量 | 8/10 | 摘要指出真实改动面和多个待验证边界；confidence 0.62，仍需 Planner / Reviewer 查证。 |
| Planner 工具选择与证据收集 | 8/10 | 22/22 diff 覆盖、调用均成功、没有精确重复；相近搜索可以减少。 |
| 调查阶段循环控制 | 8/10 | 未见工具调用原地循环；轮数仍全部花在调查，没有最终产物。 |
| FinalizeReview 兼容与收尾 | 1/10 | 命名强制工具选择被 thinking mode 拒绝；两次网络请求后终结失败。 |
| 失败重试控制 | 4/10 | 外部请求上限实际守住，但对不可重试的参数错误及本地预算耗尽又做了 4 次空转尝试。 |
| Usage / cost 可观测性 | 2/10 | session 把 Planner tokens 记成 0，不能从本次轨迹恢复实际用量或成本。 |
| **端到端 Harness 验收** | **4/10** | 有价值的调查已经完成，但没有 schema-valid Plan 被接纳，核心工作流仍未闭环。 |

结论：本次不支持把该大 PR 的 Planner 成功率或 Recall 计为通过；但它清楚证明了调查工具链可以稳定覆盖全部 diff 并向关联实现扩展。下一次真实复测前应修正两点：DeepSeek 的 FinalizeReview 请求需在终结阶段显式关闭 thinking mode；HTTP 400 和本地终结请求预算耗尽必须作为不可重试错误停止。还需修复 Planner usage 记录，再用这个用例确认 Plan 接纳后才评估维度覆盖与成本。

## 2026-09-23 补充：失败重试修复与最终 Plan 接纳

上节记录的是旧运行的失败，不代表本次最终状态。使用相同的 `ollama__ollama@b6002f6` case、16 轮 Planner 上限和真实 API 配置复测后，Plan 已被 Harness 正式接纳。最终产物位于：

- `.codesage/deep-review-smoke-25.4-ollama-rerun-20260923i/ollama__ollama@b6002f6/summary.json`
- `.codesage/deep-review-smoke-25.4-ollama-rerun-20260923i/ollama__ollama@b6002f6/report.json`
- `.codesage/deep-review-smoke-25.4-ollama-rerun-20260923i/ollama__ollama@b6002f6/events/e9821f36846b/events.jsonl`
- `.codesage/deep-review-smoke-25.4-ollama-rerun-20260923i/ollama__ollama@b6002f6/sessions.sqlite3`

最终运行耗时 78.44 秒，状态为 `planning_completed`。模型提交的 8 个 Plan dimensions 经 `ReviewPlanDraft` Schema 接纳后，由确定性 Repair 得到 9 个 active dimensions；22 个待审文件均被纳入，`coverage_complete=true`，没有 deferred dimension。12/12 个参考评论路径落在 active dimensions 中。这个指标只表示规划路径覆盖，不表示实际 Finding Recall，因为本轮仍未执行 Reviewer。

### FinalizeReview 的故障原因与修复

`tool_choice=FinalizeReview` 是发给模型供应商的强制选择参数，不是应用侧对模型返回值的本地授权边界。旧实现虽只在终结阶段注册 `FinalizeReview`，但会把调查阶段的 `tool_use` 请求历史转换成普通文本重新放入模型上下文；同时原 Planner 系统提示仍包含调查工具说明。真实会话中模型因此返回 `file_read` / `code_search`，Tool Gateway 将其标记为 `Unknown tool`，没有执行这些未注册调用，但它们消耗了终结轮次。

单独替换终结阶段系统提示不足以消除历史工具请求的诱导。当前终结请求会使用独立的最小系统提示，移除旧的 `tool_use` 请求文本，把已有 `tool_result` 保留为通用历史证据，并只向模型提供 `FinalizeReview` 工具。强制目标工具未出现在活动 Schema 时，本地立即失败关闭，不再静默退化成普通工具选择。最终成功运行中 SQLite 仅有一次 `FinalizeReview` 调用，状态 `completed`、`completion_mode=finalize_tool`，且返回了结构化 `final_payload`。

调查上下文整理后，下一次轨迹中模型确实选中了 `FinalizeReview`，但 Draft 被本地校验拒绝：第一份 payload 的 summary 为 1011 字符，relation 为 345 字符；纠错后 relation 仍为 321 字符。原 Schema 的 summary 1000 / relation 300 字符上限与当前 Plan 内容不匹配，也没有在 Planner 提示中明确长度要求。将 Draft 上限调整为 summary 2000、relation 500，并在 Planner 提示中明确约束后，最终运行一次提交即被接纳；没有增加终结请求预算。

模型流错误现在按状态码与类别区分：HTTP 400、认证/配置/配额错误和本地 FinalizeReview 请求预算耗尽不再进行无意义重试；短暂连接错误仍由 Harness 在有界预算内处理。DeepSeek 的 thinking 只在强制终结请求中关闭，旧审查路径保持原样。当前 `.env` 仍使用 `LLM_PROVIDER=openai` 搭配 DeepSeek base URL；这次成功运行没有改动该配置，因此 provider 标签不能单独解释此前的越界工具调用。

### 最终用量和验收边界

| 阶段 | Input tokens | Output tokens | Total tokens | cost |
| --- | ---: | ---: | ---: | --- |
| Semantic | 9,141 | 3,948 | 13,089 | 未提供 |
| Planning | 20,523 | 3,318 | 23,841 | 未提供 |

供应商没有返回可用的 `cost_usd`，因此这里不估算价格。最终运行证明 Semantic → Planner → FinalizeReview → Plan Repair 的闭环可完成；不证明 Reviewer、Cross Analysis 或 Finding Recall 已验收。

最终定向回归使用工作区内的 pytest basetemp，`test_bridge_deep_runtime.py`、`test_package_25_4.py`、`test_query_loop.py` 和 `test_llm_service.py` 共 114 项通过。此前未指定 basetemp 的一次测试遇到系统 Temp 目录权限错误；改用工作区临时目录后完整通过。另有 3 条既有 Pydantic deprecation warnings。
