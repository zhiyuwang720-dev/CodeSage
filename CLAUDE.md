# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览



## 命令



## 架构(大图)



### 核心设计不变量(违反需先改主规格)



### 目录规范(主规格强制,所有阶段遵守)



## 工作流约定

每个改动(代码/文档/配置)一律走**编号化 plan→spec**,禁止跳级直接开写:

1. **先 plan**:产出方案(可用 AskUserQuestion 澄清)→ 用户**明确批准**后才算数。
2. **编号**:plan 与 spec 共用同一工作项序号,**自 05 起**接续(00-04 已用于既有阶段 spec);同一工作项的 plan 与 spec 同号配对。
3. **落盘**:批准后 plan 编号写入 `docs/plan/NN-<slug>.md`;实施方案前写编号 spec 到 `docs/spec/NN-<slug>.md`。
4. **实现**:按 spec 实现 → 测试全绿 → 交付(文档随代码同交付)。

- 架构/方向类分析产物放 `docs/architecture/`;模块级说明(可选)放 `docs/modules/`。
- 历史无编号 ad-hoc spec(diff落盘/PR流程改造/PR语义改造)不回补编号。
- 会话中 ponytail(懒人)模式激活;文档与交流用中文。

## Boundaries

- **Always**: 每个改动先出 plan 并经用户批准;编号从 05 起;测试全绿再合;文档与代码同交付
- **Ask first**: 新增依赖;跨工作项改动(超出已批准 plan 的范围);改 pyproject 元数据
- **Never**: 未经批准执行改动;修改 Kode-CLI 或 backend 旧代码;提交 API key/密钥
- **master 提交**: 默认走分支+合并;紧急修复/纯文档批次经用户明确拍板后可直接提交 master(近期实践)
