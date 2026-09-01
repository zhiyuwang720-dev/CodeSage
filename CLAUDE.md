# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览



## 命令



## 架构(大图)



### 核心设计不变量(违反需先改主规格)



### 目录规范(主规格强制,所有阶段遵守)



## 工作流约定

- master 永远只收合并;每阶段一个 `feat/0N-xxx` 分支(自 master 切)
- 每阶段交付:代码 + `docs/specs/0N-*.md` + `docs/modules/0N-*.md` + 测试全绿 + 合并 push
- 实现顺序按 `tasks/todo.md`;完成一个阶段勾选一个,合并后同步 `feat/0N-xxx` 分支与 master 一致
- 会话中 ponytail(懒人)模式激活;文档与交流用中文

## Boundaries

- **Always**: 阶段开始先读该阶段规格;测试全绿再合并;文档与代码同 PR
- **Ask first**: 新增依赖;调整阶段顺序或模块边界;改 pyproject 元数据
- **Never**: 直接提交到 master(只能合并);修改 Kode-CLI 或 backend 旧代码;提交 API key/密钥
