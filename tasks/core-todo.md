# CodeSage Agent Core 重构任务清单

> 依据 `docs/specs/agent-core.md`(边界与设计理念)。目标:最稳定、零冗余的 Agent Core 包,现有功能迁为扩展包。
> 分支约定:`refactor/core-0N`。每步验收:核心测试绿 + 现有语义保留。

## 现状 → 目标映射

| 现有模块 | 去往 |
|---|---|
| `codesage/core/`(messages/normalize/session) | → Core `agent/contract/` + `agent/session/` |
| `codesage/engine/`(loop/tool_queue) | → Core `agent/runtime/`(重构为双层循环+事件流+三阶段管道) |
| `codesage/ai/`(client/adapters/retry/cost/vcr) | → 扩展 `codesage/llm/`(实现 StreamFn) |
| `codesage/permissions/` | → 扩展 `codesage/perm/`(实现 beforeToolCall hook) |
| `codesage/tools/` | → 扩展 `codesage/tools/`(实现 Tool 契约) |
| `codesage/cli/` | → 扩展 `codesage/cli/`(消费 AgentEvent) |

## 任务(依赖排序)

- [ ] **CT-01 包结构重构**(`refactor/core-01`)
  - 建立 `codesage/agent/{contract,runtime,session}/`;现有代码按映射迁入,不改变行为
  - 验收:迁移后 382 测试全绿(仅改 import 路径);`codesage/agent` 无 cli/ui 依赖
- [ ] **CT-02 契约层收敛**(`refactor/core-02`)
  - `contract/message.py`:ContentBlock/SessionMessage 收敛(ai/types + core/messages 合并,单一权威)
  - `contract/tool.py`:Tool 扁平契约 + ToolResult 增加 **terminate** 字段(全批同意才停)
  - `contract/stream_fn.py`:StreamFn 协议(错误入流,`stopReason: error/aborted`)
  - `contract/event.py`:AgentEvent 四级生命周期(agent/turn/message/tool)
  - 验收:契约类型单测;现有消费方(扩展包)适配后全绿
- [ ] **CT-03 loop 重构:双层循环 + 事件流 + 工具三阶段**(`refactor/core-03`)
  - 双层 while(内层:工具 + steering 注入;外层:follow-up 轮询)
  - 工具管道 prepare(校验+权限钩子,`{block:true}` 拒绝)→ execute(并行,结果按源顺序)→ finalize(改写口)
  - `stopReason=="length"` → fail 全部工具调用(残缺参数防执行)
  - 事件流:每轮产出 AgentEvent(tool_execution_start/update/end)
  - 验收:循环单测(终止/中断/截断/block/terminate)+ >2000 轮压力测试
- [ ] **CT-04 Agent 状态封装**(`refactor/core-04`)
  - `runtime/agent.py`:state(systemPrompt/model/tools/messages/isStreaming/pendingToolCalls/errorMessage)+ steering/followUp 双队列(all/one-at-a-time)+ subscribe + abort/waitForIdle + 单飞(activeRun)
  - 失败转完整事件序列(handleRunFailure 模式)
  - 验收:状态/队列/订阅单测
- [ ] **CT-05 Session entry 链 + 操作日志**(`refactor/core-05`)
  - `session/store.py`:Entry(消息/model 变更/thinking 级别/活动工具/compaction)+ Record 操作日志(operation_started/tool_started/step_attempt)+ lane 指针
  - torn-tail 恢复 + findOpenOperations(从中断点恢复,而非消息末尾)
  - 验收:entry 链单测(追加/fork/恢复)+ 崩溃恢复测试
- [ ] **CT-06 hooks 管道**(`refactor/core-06`)
  - `runtime/hooks.py`:beforeToolCall(可 block/改写参数)/afterToolCall(可改写结果);权限扩展包通过此接入
  - 验收:hooks 单测(block/改写/顺序)
- [ ] **CT-07 扩展包拆分**(`refactor/core-07`)
  - `codesage/llm/`:ai 迁移(StreamFn 实现 + 模型指针/重试/取消/成本/VCR)
  - `codesage/perm/`:权限引擎迁移(决策链 + 审计,实现 beforeToolCall)
  - `codesage/tools/`、`codesage/cli/`:迁移为事件流消费者
  - 验收:全量测试绿;Core 包零依赖扩展包(反向依赖)
- [ ] **CT-08 错误恢复阶梯(Core 增强)**(`refactor/core-08`)
  - 可恢复错误先扣留 → 恢复阶梯(compaction/升级重试)→ 防死循环闸;显式状态对象 + transition reason
  - 验收:恢复路径单测(413/max_output_tokens 场景)
- [ ] **CT-09 测试全量迁移 + 语义验证**(`refactor/core-09`)
  - 所有测试按新包结构迁移;语义对照表(旧测试 → 新测试)确保无行为丢失
  - 验收:全量绿 + V1 验收(真实 API)通过 + 压力测试通过
- [ ] **CT-10 文档收尾**(`refactor/core-10`)
  - `docs/modules/agent-core.md`:设计决策剖析 + 面试问题(沿用既有格式)
  - 更新主规格(路线图以 Core 为起点)与 CLAUDE.md
  - 验收:文档与实现一致;todo 全勾选

## 验证检查点

1. 每步:该步测试绿 + 382 基线不退化
2. CT-03 完成:>2000 轮压力测试
3. CT-09 完成:真实 API 验收(创建文件/deny 拦截/审计)复跑通过
4. CT-10 完成:Core 包零 UI 依赖可被任意前端驱动(最小演示:脚本直连 Core 跑一轮)
