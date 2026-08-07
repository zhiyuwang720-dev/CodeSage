<p align="center">
  <img src="assets/logo.png" alt="CodeSage" width="120"/>
</p>

# CodeSage

你有没有过这种感觉——问某个 AI 工具一个复杂代码库的问题,它给你扔过来一篇 500 页的论文,根本看不下去?CodeSage 正好相反。它用最简单的人话解释复杂项目,就像坐在你旁边的资深工程师。而且它写代码的时候,只写最少的代码来完成工作。不过度设计,不堆砌废话。只有清晰和高效。

## CodeSage 是什么

CodeSage 是一个用 Python 实现的、类 Claude Code / Kode-CLI 的 Harness 框架——分阶段结构化重建,两个目的:

1. **学习**:逐模块复刻 harness,端到端理解每个部件如何工作
2. **未来适配**:为大型项目编写与安全领域适配(权限、审计、沙箱)打下坚实基础

## 当前状态

| | |
|---|---|
| 阶段 01–09 已交付 | 配置、AI 客户端、工具、消息与会话、权限、引擎主循环、CLI、上下文、钩子系统 |
| 测试 | 793 通过 / 9 跳过(LLM 集成测试,无 key 自动跳过) |
| 技术栈 | Python ≥ 3.11 + asyncio、httpx、pydantic |

每个阶段 = 一个模块 + 一个独立分支,测试全绿后才合并回 `master`。

## 快速开始

```bash
# 运行全部测试(仓库根或 codesage/ 内均可)
python -m pytest codesage/tests/ -q

# 运行单模块
python -m pytest codesage/tests/hooks/ -q

# 查看版本
python -m codesage.cli --version

# 含 LLM 集成测试(无 key 自动跳过)
DEEPSEEK_API_KEY=xxx python -m pytest codesage/tests/ -q
```

## 目录结构

```
codesage/            # 生产级 harness(唯一活跃代码)
  config/            # 配置系统:settings 三层(user/project/local)+ 全局配置
  ai/                # LLM 客户端:双 adapter、重试、成本、模型指针、VCR
  tools/             # 工具契约 + 注册表 + 12 个内置工具
  core/              # 消息与会话
  permissions/       # 权限决策链:deny > ask > allow,审计
  engine/            # 引擎主循环、压缩、工具队列
  cli/               # 交互式 REPL
  context/           # AGENTS.md 收集、system prompt 组装
  hooks/             # 八事件钩子:command/prompt/http 三执行体 + if 条件
Kode-CLI/            # 参考实现(TypeScript,只读)
docs/                # intent / ideas / specs / modules 四层文档,规格驱动开发
```

## 文档

- `docs/specs/codesage.md` — 主规格:19 阶段路线图、核心设计不变量
- `docs/specs/0N-*.md` — 各阶段规格(实现某阶段前必读)
- `docs/modules/` — 每阶段理解文档
- `tasks/todo.md` — 任务清单与验收标准

## 路线图

| # | 阶段 | 状态 |
|---|---|---|
| 01 | 配置系统 | ✅ 已交付 |
| 02 | LLM 客户端 | ✅ 已交付 |
| 03 | 工具 | ✅ 已交付 |
| 04 | 消息与会话 | ✅ 已交付 |
| 05 | 权限引擎 | ✅ 已交付 |
| 06 | 引擎主循环 | ✅ 已交付 |
| 07 | CLI REPL | ✅ 已交付 |
| 08 | 上下文管理 | ✅ 已交付 |
| 09 | 钩子系统 | ✅ 已交付 |
| 10 | 上下文压缩 | 下一个 |
| 11–19 | 任务、会话、子代理、技能、MCP、Bash 安全、记忆、多模型、插件 | 规划中 |

## 许可证

尚未授权 —— 复用前请先询问。
