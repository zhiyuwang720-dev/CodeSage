# CodeSage 任务清单

> 对应 `docs/specs/codesage.md` 阶段路线图。每个任务 = 一个分支 + 阶段规格 + 实现 + 文档 + 合并。

- [ ] **01 config 配置系统** (`feat/01-config`)
  - 验收:settings 三层(user/project/local)加载与覆盖正确;全局配置读写;AGENTS.md 路径发现
  - 验证:`pytest tests/ -q`;针对覆盖优先级的单测
- [ ] **02 ai LLM 客户端** (`feat/02-ai`)
  - 验收:OpenAI 兼容 + Anthropic 原生双 adapter;流式;重试(retry-after);模型指针 main/task/compact/quick;VCR 录制回放;成本核算
  - 验证:单测(离线 mock)+ VCR 回放测试
- [ ] **03 tools 工具契约与内置工具** (`feat/03-tools`)
  - 验收:Tool 三合一对象;注册表;Read/Write/Edit/Glob/Grep/LS/Bash(真超时/kill);超大结果落盘
  - 验证:每工具单测;Bash 超时/kill 实测
- [ ] **04 core 消息与会话** (`feat/04-core`)
  - 验收:Message 类型;normalizeMessagesForAPI(合并/剔除);会话 JSONL append-only + 原子写
  - 验证:归一化规则单测(含边界:相邻 user 合并、tool_result 前置)
- [ ] **05 permissions 权限引擎** (`feat/05-permissions`)
  - 验收:决策链完整顺序;deny>ask>allow;路径规则(gitignore 语义 + symlink 展开);写保护路径;plan/default/yolo 三模式;**审计钩子**
  - 验证:决策链矩阵单测;审计事件断言
- [ ] **06 engine 引擎主循环** (`feat/06-engine`)
  - 验收:主循环(递归或显式迭代,见 R1);ToolUseQueue 并发屏障;错误转 tool_result;AbortSignal 三检查点;hooks 挂接点
  - 验证:循环终止单测;**>2000 轮压力测试**
- [ ] **07 cli CLI REPL** (`feat/07-cli`)
  - 验收:交互循环;权限询问(文本);信号处理;流式输出 → **V1 闭环验收**
  - 验证:端到端小任务;V1 验收清单(见 plan.md)
- [ ] **08 context 上下文管理** (`feat/08-context`)
  - 验收:AGENTS.md 逐层收集 + 32KB 截断 + override;system prompt 分层组装;system-reminder(上限 10)
  - 验证:上下文组装单测
- [ ] **09 hooks 钩子系统** (`feat/09-hooks`)
  - 验收:PreToolUse(汇入权限)/PostToolUse/Stop/UserPromptSubmit/SessionStart;命令 + 提示双执行体;JSON 解析;exit 2 硬阻断
  - 验证:每事件类型单测
- [ ] **10 compact 上下文压缩** (`feat/10-compact`)
  - 验收:token 预算;auto-compact(LLM 摘要);micro-compact(超大结果 → 400 字符 + 落盘)
  - 验证:压缩边界单测
- [ ] **11 tasks 任务系统** (`feat/11-tasks`)
  - 验收:Task CRUD;blocks/blockedBy 环检测;todo
  - 验证:依赖图单测(环/缺失)
- [ ] **12 session 会话生命周期** (`feat/12-session`)
  - 验收:fork/continue/resume;sidechain 日志;归档
  - 验证:会话恢复单测(摘要前 2 条 user 消息)
- [ ] **13 subagents 子代理** (`feat/13-subagents`)
  - 验收:agent 定义(frontmatter + 优先级合并);Task 工具;forkContext;前/后台;禁递归工具
  - 验证:嵌套调用单测
- [ ] **14 skills 技能系统** (`feat/14-skills`)
  - 验收:skill 发现/加载/契约;allowed_tools;斜杠命令机制
  - 验证:技能加载与权限联动单测
- [ ] **15 mcp MCP 客户端** (`feat/15-mcp`)
  - 验收:stdio/HTTP 传输;mcp__ 命名 + needsPermissions 强制;resources;OAuth PKCE
  - 验证:mock server 单测
- [ ] **16 bash-safety Bash 安全纵深** (`feat/16-bash-safety`)
  - 验收:破坏性守卫;LLM 意图闸门(fail-closed);沙箱计划接口(Windows 文档化降级)
  - 验证:守卫规则单测;闸门 mock 查询单测
- [ ] **17 memory 记忆系统** (`feat/17-memory`)
  - 验收:JSONL 事件溯源;保守提取(显式标记);本地词法检索;注入标注不可信
  - 验证:提取/检索单测
- [ ] **18 multimodel 多模型编排** (`feat/18-multimodel`)
  - 验收:专家模型(AskExpertModel 类);辅助回退增强;上下文感知切换(90% 预算)
  - 验证:回退链单测
- [ ] **19 plugins 热插拔注册层** (`feat/19-plugins`)
  - 验收:模块注册表;≥2 实现切换零代码侵入;插件化工具/技能/MCP 统一入口
  - 验证:切换单测 + 最终全量回归

## 收尾

- [ ] V1 验收(07 完成时)
- [ ] 最终回归 + 项目 README(19 完成后)
