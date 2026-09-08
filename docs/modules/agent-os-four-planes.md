# CodeSage Agent OS 四层模块边界

## 依赖方向

```text
api ──command/query──> control_plane ──ports──> infrastructure
                           ▲                         │
                           │ result/ownership        │ ARQ/PostgreSQL/Redis
execution_plane ───────────┘                         │
       │                                             │
       ├──> domains/pr_review                        │
       └──> tool_gateway ────────────────────────────┘
```

- 接入层只做 HTTP/SSE/Webhook 协议转换，不执行 QueryLoop。
- 控制平面拥有生命周期、delivery、lease/epoch、策略快照和 fenced 结果事务。
- 执行平面领取受管任务、续租、执行/恢复 Harness，并通过控制面结果端口提交。
- PR 领域拥有 diff/规则/提示词/三视角/综合语义，不依赖框架与持久化。
- ToolGateway 是模型工具调用唯一入口，顺序为 schema、策略、执行、输出治理与回执。

## 迁移映射

| 原位置 | 新位置 |
|---|---|
| `services/agent/task_queue,event_*` | `infrastructure/messaging` |
| `services/agent/task_executor,json_parser` | `execution_plane` |
| `services/agent/core/errors,retry` | `execution_plane/models` |
| `services/agent/prompts` | `domains/pr_review/prompts.py` |
| `services/pr_review/lifecycle,execution_ownership,results,inputs` | `control_plane` |
| `services/pr_review/execution,quick_review,*dispatcher` | `execution_plane/review` |
| PR 规则、编排、综合、diff | `domains/pr_review` |
| `services/tooling,permission` | `tool_gateway` |
| `services/runtime,llm,session` | `execution_plane`；DB stage store 位于 `infrastructure/persistence` |
| `services/contracts` | `contracts` |

## 权威状态与记录

- `ReviewExecutionRun`、StageResult、Findings、ArtifactRef 和任务终态是业务 fenced 业务事务的业务真相。
- Session/Message/Turn/Checkpoint 是恢复事实，不能由采样 Trace 替代。
- `AuditModelStreamAttempt` 只用于诊断，19B 在恢复回归先通过后删除。
- `audit_tool_calls` 保留为 ToolExecutionReceipt 的物理兼容表；不是 exactly-once 副作用账本。
- AgentEvent 只服务展示，不作为性能、成本或提交真源。

## 19A 基线

- 基线提交：`8a2cb9b9d431a7dde46e6655146179583747c8e1`。
- PR 提示词 SHA256：`0FEFB6E19C01F8697316BF55F7DEA9DC574F1265DE8DB7A53DD054AE29C03429`，迁移后相同。
- 相关测试：430 passed；既有 3 failures 为 Windows Bash 能力差异两项及嵌套 Write 文本解析一项。
- 生产 ARQ job timeout 为 3600 秒；验收故障 profile 仍显式使用 60 秒。
