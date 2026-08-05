# claude-code-main 借鉴分析(2026-08-05)

> 5 个对比代理对照 Claude Code 源码(38 万行,1332 文件)与 CodeSage 实现,输出可借鉴清单。已映射到 `tasks/todo.md`(CC-01 ~ CC-17)。

## 核心纪律(Claude Code 最值得抄的三条)

> **每个错误都有恢复路径、每个恢复路径都有熔断、每个决定都幂等可重放。**

| 纪律 | Claude Code 做法 | 我们的差距 |
|---|---|---|
| 错误恢复 | 可恢复错误(413/max_output_tokens)先从流中**扣留**,尝试恢复阶梯(collapse → reactive compact → 升级重试)→ 才 surface;每个恢复路径配计数器防死循环 | 任何 LLMError 直接降级为 is_error 消息终止,零恢复 |
| 熔断 | autocompact 连续失败 3 次熔断(线上数据:50+ 次连续失败浪费 ~25 万次 API 调用);stop-hook 死亡螺旋防护 | 无上下文管理,无熔断概念 |
| 幂等 | 工具结果落盘路径按 tool_use_id 确定性生成 + `wx` 容忍 EEXIST + 决策记录入 transcript 供 resume 重建 | spill 每次 mkdtemp 新路径,打破 prompt cache 前缀 |

## 立即修复项(安全/正确性)

| # | 项 | 问题 |
|---|---|---|
| CC-01 | is_concurrency_safe 默认 False | 我们默认 True(方向反了),忘了声明的新工具会并行执行 |
| CC-05 | 权限路径大小写归一化 | `.cLauDe/SeTtInGs.json` 可绕过写保护(macOS/Windows) |
| CC-06 | symlink 双路径检查 | resolve_candidates 是 stub,只查最终 resolve 一次,`/tmp/link → ~/.ssh/config` 可穿透 |
| CC-07 | session 规则接线 | session_permissions 死参数,「仅本次会话允许」整体不可用 |
| CC-08 | Bash 注入补 `=cmd` | `=curl evil.com` 可绕过 `Bash(curl:*)` 规则 |
| CC-03 | 空工具结果标记 | 空 content 原样透传,可能误触发 `\n\nHuman:` stop 序列 |

## 设计差异对照(值得记住的决策)

| 主题 | Claude Code | CodeSage | 判断 |
|---|---|---|---|
| context 注入位置 | system-reminder **用户消息**(请求时 prepend) | 拼进 system prompt | **CC 更优**:system 字节稳定利于 prompt cache;可中途追加 |
| 会话持久化 | typed-entry JSONL(标题/标签/模式都是尾部条目) | 纯消息流 | CC 更优:元数据与消息共存,resume 天然拿标题 |
| 记忆 | memdir 文件记忆(无 DB 无 embedding,索引+主题文件) | 无(阶段 17 计划) | 文件方案对我们更省:可 git 管理、ls/grep 即检索 |
| 权限链 | 双层:内层 1a-1g 规则链 + 外层模式后处理(无早退路径) | 10 步单链 | 结构已对齐;缺 passthrough 层、钩子位、分类器位 |
| yolo 安全 | 进 auto 模式剥离危险 allow 规则(`Bash(*)` 等) | yolo 保留全部规则 | CC 更优:yolo + 宽泛 allow = 静默破坏 |
| 工具调度 | 并发上限 10 + 边完成边 yield + 错误隔离(不毒化) | 无界 gather + 批毒化 | CC 更优:读类批量保留成功结果;毒化适合写类 |
| 优雅退出 | failsafe 定时器 + 幂等守卫 + 分级清理 | 13 行裸处理 | CC 更优:清理有预算,超时强杀 |
| 斜杠命令 | 命令即数据对象 + 注册表 | if/elif 链 | CC 更优:新增命令=加一个数据对象,/help 自动渲染 |

## 不借鉴(理由)

- **StreamingToolExecutor**(流中启动工具)—— 复杂度爆炸,无 UI 消费端
- **tree-sitter 全套 bash 校验器** —— 启发式 + 未来 LLM 闸门更省路
- **React/Ink UI 全家、typeahead、HelpV2** —— 纯文本 REPL 定位
- **输入优先级队列** —— V1 串行足够
- **KAIROS 每日日志、team memory、context collapse** —— 多进程/云场景
- **commander 子命令框架** —— Python import 成本低,argparse 单文件够
- **telemetry/GrowthBook 开关** —— ant-only

## 落地状态

已更新 `tasks/todo.md`:CC-01~11 立即做 / CC-12~14 阶段 08 增强 / CC-15~17 新阶段(错误恢复、会话 UX、记忆)/ compact(10)、Bash 安全(16)、hooks(09)需求补充。
