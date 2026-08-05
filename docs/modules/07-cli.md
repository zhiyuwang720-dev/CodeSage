# 阶段 07 — CLI REPL 与 V1 闭环理解文档

> 分支 `feat/07-cli`,规格见 `docs/specs/07-cli.md`。**V1 主线完成站。**

## 模块职责

终端前端:把 01–06 装配成可用产品。两种模式:
- **交互 REPL**(`codesage` 或 `python -m codesage.cli`)—— 逐轮对话
- **单次模式**(`codesage "任务"`)—— 非交互,ask 决策拒绝(安全默认),验收自动化走这条路

## 关键设计决策

### 1. 装配根(composition root)

`assemble.build_loop()` 是唯一装配点:settings → LLMClient → 工具注册表 → 权限引擎(审计 sink)→ Session → AgentLoop。阶段 06 的 AgentLoop 完全依赖注入,CLI 只是把线接好 —— 阶段 19(热插拔)改造的正是这一处。

### 2. 单次模式的权限语义:ask → 拒绝

单次模式没有 UI,`request_permission=None` —— ask 决策直接变拒绝(引擎默认)。**这产生了真实有趣的 agent 行为**:模型收到 "Permission denied: Bash requires explicit approval" 后,自动调整策略 —— 冒烟实测中它放弃执行,改为给用户输出命令建议。这是「错误自愈」设计在真实世界的完整演示。

### 3. 权限询问(y/n/remember)

交互模式:`request_permission` 接终端 —— `(y)es / (n)o / (r)emember`。**remember 写入 settings.local.json 的 permissions.allow**(阶段 05 的 store),下次同工具直接放行。这是权限系统的人机闭环:ask → 批准 → 记住 → 规则沉淀。

### 4. env 配置(CODESAGE_MODEL / CODESAGE_BASE_URL / CODESAGE_API_KEY_ENV)

CLI 的默认(main)profile 可用环境变量指向任意 OpenAI 兼容端点 —— 零配置文件即可用:
```bash
CODESAGE_MODEL=deepseek-v4-flash CODESAGE_BASE_URL=https://api.deepseek.com/v1 \
CODESAGE_API_KEY_ENV=DEEPSEEK_API_KEY codesage "任务"
```
全局配置(GlobalConfig.model_profiles)是进阶路径,env 是 V1 的快捷路径。

### 5. 打包:console script + 包内 __main__

修了一个真实工程坑:`codesage/cli/__main__.py`(顶层入口)与包内 `codesage/codesage/cli` 同名冲突 → `python -m codesage.cli` 报 "is a package"。正解:入口放包内 + `pip install -e .` + pyproject `[project.scripts] codesage = "codesage.cli:main"`。标准打包路径,CLI 以后就是可安装产品。

## 冒烟实测(真实 API)

```
You: 回复 V1 冒烟测试通过即可
◈ LS path=...
◈ Glob pattern=**/*test*
  (thinking: 382 chars)
  ✓ tool[Ia3689] .pytest_cache/ codesage/ ...
  ✓ tool[qP6363] tests tests/conftest.py ...
◈ Bash command=cd ... && python -m pytest ...
  ✗ Permission denied ...
```

模型**自主执行**了 LS → Glob → Bash 的探索链,并在权限拒绝后优雅调整 —— 一个真实的 agent 循环在 0.01 版本上跑起来了。

## 完成标准(对照规格,全部实测)

- [x] 单次模式:真实 API 创建文件任务完成(`test_v1_acceptance_create_file` 通过)
- [x] deny 规则:任务不执行,模型收到拒绝(`test_v1_acceptance_deny_blocks` 通过)
- [x] 审计记录存在(Write allow + Write deny 事件断言)
- [x] 交互 REPL(手动冒烟:`--mode`/`--show-thinking`/斜杠命令可用)
- [x] 170 项全量单测绿

## 已知简化(ponytail)

- thinking 默认摘要显示(`--show-thinking` 全显)—— 阶段 08 上下文感知后优化
- 渲染纯文本 + 基础 ANSI,无 rich —— 富 TUI 明确 out-of-scope
- Windows 上 SIGINT 在 input() 期间被吞 —— 轮间 abort 检查兜底
- `/show-thinking` 需重启生效(仅演示 toggle,无状态持久化)

## 阶段衔接

- 阶段 08(context):base_prompt 换完整分层组装 + AGENTS.md
- 阶段 09(hooks):权限询问可被 PreToolUse 钩子改写
- 阶段 12(session):`--resume` 在 session 存储上叠加
- 阶段 15(mcp):装配根接入 MCP 工具注册
- 阶段 19(plugins):装配根 = 热插拔改造点
