# 16 小范围底座验收记录

> Plan 18 的最新结构化验收见 [PLAN18_RESULTS.md](PLAN18_RESULTS.md)。本文件保留历史结果，不作为 Plan 18 通过依据。

- 基线提交：`61c8d7a36e19ce8c3fe8a03c94287cfe1a5ca9be`
- 环境：PostgreSQL 16、Redis 7、队列 `codesage:acceptance:agent_tasks`
- 一键验收：13 passed in 6.37s
- 独立 worker：`acceptance:7176`、`acceptance:37944`
- 双 worker 断言：两个不同 PID 均领取并执行任务，执行区间重叠，且各自实际调用一次只读 `Read` 工具。
- 所有权断言：有效 lease 排他、数据库取消阻止续租、resume 保持 run_id 并更换 attempt/epoch、旧 epoch 提交被拒绝。
- 产物断言：安全相对路径、SHA-256/大小一致性、正文破坏后拒绝读取。
- 清理：测试结束后 PostgreSQL、Redis 容器及 pytest 临时产物已清理；失败时脚本保留日志。

仍需后续工作项覆盖：确定性模型完整穿过 QueryLoop/SessionStore 的故障矩阵、跨进程取消后阶段复用和强制终止自动接管的连续三次验证。
