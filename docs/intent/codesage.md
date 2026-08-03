# CodeSage — 项目意图声明

> 来源:interview-me 访谈(2026-08-03),已获用户明确确认。

## 意图

- **OUTCOME**: 一个 Python 编写的、类 Claude Code / Kode-CLI 的 Harness 框架(CodeSage),模块化分阶段开发,每阶段一个模块、做到当前最大深度,并产出一份理解文档(中文,供复习)
- **USER**: 用户本人 —— 先通过复刻理解 harness 架构,再改造用于大型项目编写和安全领域适配
- **WHY NOW**: LLM 模块已成(保留精修),agent/runtime 等是半成品,方向已验证,需要系统化阶段计划
- **SUCCESS**:
  - V1 = 最小闭环(终端 REPL + 单模型 + 核心工具 + 权限门控,端到端完成一个小任务)
  - V2+ = 每个模块全量完成,系统始终可运行
  - 最终 = 热插拔注册层阶段收尾
- **CONSTRAINT**:
  - master 永远合并最新;每模块一个编号分支
  - LLM 模块保留(精修),其余模块重做/精修
  - MCP 保留客户端侧
  - OpenAI 兼容协议接入(DeepSeek/Qwen/GLM 等)
- **OUT OF SCOPE**: 桌面/Web/VS Code/Server 客户端;富 TUI(图片渲染、行内编辑);MCP 服务器端;商业化、多人协作、云服务

## 关键决策记录

1. **热插拔**: 作为专门后期阶段,不从开始预埋插件框架;V1 起守住「模块边界干净」纪律,后期只是加注册层
2. **阶段粒度**: 每阶段一个模块做到最大深度,同时系统保持可运行;每阶段交付 = 模块 + 文档
3. **分支策略**: 每模块一个编号分支(feat/01-xxx 等),master 永远合并最新
4. **已有代码定位**: LLM 模块保留精修;agent/runtime 为半成品,由阶段计划重做覆盖
5. **参考项目**: 本机 `/e/Mac/github/Kode-CLI`(TypeScript monorepo,packages: agent/ai/config/context/core/engine/hooks/host + apps/cli)
