# V1 文件级差距判断(2026-08-05,团队 7 代理)

> 对照 Kode-CLI 每个模块的文件/功能点,分类:A=当前必须实现 / B=后续阶段(映射路线图)/ C=有意不做。

## 结论:当前必须实现(A 类)共 19 项

### permissions(4 项,安全优先)

| # | 项 | 优先级 | 问题 |
|---|---|---|---|
| P1 | **规则字符串解析 Tool(content) 语义** | P0 | `allow:["Read(/abs/**)"]` 整串被当工具名 fnmatch → **路径约束全丢,放行任意路径**,且先于写保护检查 |
| P2 | **Bash 精确规则 + 子命令级评估** | P0 | remember 落整工具名 → 记住一条命令后模型可跑任意命令 = **可绕过** |
| P3 | **remember 落精确粒度** | P0 | `Bash(<cmd>)`/`Edit(<path>/**)`,A2 的用户侧闭环 |
| P4 | **Windows 路径守卫补全** | P1 | 尾部点/空格(`Write("settings.json. ")` 实际写 settings.json)、NTFS ADS、`\\?\` 前缀 |

### ai(2 项,传输正确性)

| # | 项 | 优先级 | 问题 |
|---|---|---|---|
| A1 | **流中取消贯穿 HTTP + retry sleep** | 高 | R2 计划承诺;首次 Ctrl+C 最长挂 300s(httpx read timeout) |
| A2 | **截断流丢弃部分 tool_use + 空流重试** | 高 | 流中断时残缺 JSON 工具输入**被执行**(写类工具事故路径) |
 
### tools(3 项)

| # | 项 | 优先级 | 问题 |
|---|---|---|---|
| T1 | **Grep rg 化** | 高 | 纯 Python 大仓分钟级 vs 秒级,且不读 .gitignore(vendor 目录拖垮会话) |
| T2 | **TodoWrite** | 中 | 多步任务跟踪主机制,纯工具层 ~100 行 |
| T3 | **WebFetch(SSRF 防护)** | 中 | httpx 已有;缺它模型无法读文档 |

### cli(7 项)

| # | 项 | 优先级 |
|---|---|---|
| C1 | `-p/--print` + headless 自动判定 | P0 |
| C2 | `--max-budget-usd` CLI 暴露 | P0 |
| C3 | `--output-format json`(CI 解析) | P1 |
| C4 | `--debug/--verbose` 日志 | P1 |
| C5 | `--system-prompt/--system-prompt-file` | P1 |
| C6 | flag 组合校验 | P2 |
| C7 | 非交互权限语义文档化 | P2 |

### config(2 项)+ engine(1 项)

| # | 项 | 优先级 |
|---|---|---|
| CF1 | save_approval 并入 atomic_write(symlink/mode/降级) | 高 |
| CF2 | atomic_write Windows EEXIST/EPERM 重试 | 低 |
| E1 | abort 时未启动兄弟工具跳过执行 | 低 |

### core:A 类为空 ✅

## B 类主线(按路线图,不属当前)

hooks(09)→ 压缩(10)→ 任务/todo(11)→ 会话生命周期(12)→ 子代理(13)→ 技能/斜杠(14)→ MCP(15)→ Bash 纵深(16)→ 记忆(17)→ 多模型(18)→ 热插拔(19)。system-reminder/上下文分层(08)。

## C 类代表(有意不做)

daemon 域(goals/supervisor/runs 跨进程/agentEvents/stream-json 协议)、ant 内部(binary feedback)、Ink UI 全家(~300 文件)、bedrock/vertex、restrictedClientCompat 伪装、oauth 登录、cache_control(DeepSeek 自动缓存)、compat 全族、checkpoints、LSP、statusline/notificationCenter。
