# V1 七阶段生产级差距分析(2026-08-05)

> 7 个并行审查代理对照 Kode-CLI 源码逐模块审查。评级:7/7 均为「及格」,无一达到「良好」—— 确认未达生产级标准。

## 评级汇总

| 阶段 | 评级 | 一句话结论 |
|---|---|---|
| 01 config | 及格 | 地基干净,但 Windows BOM 静默丢配置 + symlink 破坏 |
| 02 ai | 及格 | 网络错误裸奔(重试/回退失效)+ 成本在真实路径恒为 0 |
| 03 tools | 及格(偏下) | 7/29 工具覆盖,Edit/Write 会静默破坏用户改动 |
| 04 core | 及格 | normalize 与 Kode wire 语义有偏差,会话无项目隔离 |
| 05 permissions | 及格(接近差) | **无工作目录约束 = yolo 任意写漏洞** |
| 06 engine | 及格偏下 | max_budget 死代码、thinking-only 静默终止 |
| 07 cli | 及格(偏弱) | CI/脚本场景 0 分,会话系统无入口 |

## 跨模块共性问题(最优先)

1. **成本死代码 bug**(ai+engine 双重确认):`LLMClient.stream()` 不累计 `total_cost`,而引擎全走 stream → **max_budget_usd 永不触发**
2. **网络错误裸奔**:adapter 不把 httpx 超时/断连包装成 LLMError → 重试/回退/fallback 全部失效(设计意图有 `status_code is None` 分支,实现缺 try/except)
3. **取消机制缺失**:abort 不贯穿 HTTP 请求(300s 悬挂无法终止)+ 工具无 abortController + 流式期间 Ctrl+C 无效

## 各模块高优先修复项

### 01 config
- [高] JSON 读取 BOM 容错(`utf-8-sig`)—— Windows 手改配置即整文件静默失效
- [中] atomic_write:symlink 目标解析 + 保留 mode + 保存失败降级不崩溃
- [中] AGENTS.md 无 git 根时回退读 cwd

### 02 ai
- [高] 传输错误包装(一处共享 helper)
- [高] stream 路径成本累计(Anthropic 流补 usage 事件 + collect 后记账)
- [中] 流式首 token 前失败重试;retry-after 上限 60s + jitter + 408/409
- [低] 错误事件消息体 bug(状态码重复);usage 归一化补 reasoning_tokens

### 03 tools
- [高] Edit/Write read-first + mtime/sha256 陈旧性校验(防静默破坏用户改动)
- [高] Bash destructive guard + cd 限制
- [高] run_in_background + TaskOutput/TaskStop(后台任务)
- [高] Read 图片/PDF + 0.25MB 输出上限
- [高] ToolUseContext 补 abortController/readFileTimestamps/readFileHashes;validate_input 接进执行链
- [中] contextModifier(cd 后 cwd 错位)、TodoWrite、WebFetch(SSRF 防护)

### 04 core
- [高] normalize 对齐 Kode:toolResultsFirst 合并序 + assistant 同 id 合并 + 空内容哨兵
- [中] 会话 key 项目作用域(sanitized(cwd)/)防止跨项目碰撞

### 05 permissions
- [高] **工作目录约束**(项目根 + 附加目录,目录外读写强制 ask)—— 堵 yolo 任意写漏洞
- [高] Bash 最小命令分析(拆分子命令 + 重定向目标 + rm 临界目标 + 注入模式 + remember 落精确 key)
- [高] 规则源分层(user/project/local/session)+ gitignore 否定语义 + 会话内存授权态
- [高] 写保护清单补全(dotfiles/.vscode/.idea/UNC/Windows 可疑路径)
- [低] 删 engine.py 死代码;Skill 移出 SYSTEM_TOOLS

### 06 engine
- [高] thinking-only 重试(注入恢复消息,3 次有界)
- [中] 超大工具结果落盘;工具级消息流契约(逐工具 yield);max_turns 数值校验

### 07 cli
- [高] 非 TTY stdin → 单轮执行(解锁 CI/脚本)
- [高] --resume/-c/--session-id(会话列表 + most_recent)
- [高] --safe + root 防护 + --allowedTools + 单轮权限逃生口
- [中] 优雅退出(SIGTERM/SIGBREAK + 退出码语义)+ /show-thinking 真开关

## 生产级达标判定(2026-08-05 修复完成)

- [x] 成本/预算在真实路径生效(stream 记账,单测覆盖)
- [x] 网络抖动不毁回合(传输错误包装 + 流首事件重试)
- [x] Edit/Write 不破坏外部修改(read-first + mtime/sha256)
- [x] yolo 不能写出工作目录(目录外强制 ask + 显式批准)
- [x] CI 可用(stdin 单轮 + 退出码语义 + --allowedTools)
- [x] 全量测试绿:269 passed, 4 skipped(环境性)
- [ ] 真实 API 验收(待 DEEPSEEK_API_KEY 恢复 —— backend/.env 缺失)

修复统计:批次 1(ai/core/config/tools)+ 批次 2(permissions/engine/cli),共 +2479 行,测试从 170 → 269(新增 ~100 项)。
