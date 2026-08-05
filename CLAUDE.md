# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

CodeSage —— 用 Python 实现类 Claude Code / Kode-CLI 的 Harness 框架,分阶段构建(学习 + 未来安全领域适配)。每个阶段 = 一个模块 + 一个编号分支,完成后合并 master 并推送 GitHub。

**权威文档(动手前先读)**:
- `docs/specs/codesage.md` — 主规格:19 阶段路线图、Kode 关键设计保留清单(20 条)、目录规划规范。改架构先改它
- `docs/specs/0N-*.md` — 各阶段规格(实现某阶段前必读对应规格)
- `tasks/todo.md` / `tasks/plan.md` — 任务清单(验收标准)与实施计划(依赖图/风险)
- `docs/modules/` — 每阶段理解文档(复习/对照用)

## 命令

```bash
# 测试(项目根 codesage/ 或仓库根均可,顶层 shim 已兼容)
python -m pytest codesage/tests/ -q                  # 仓库根运行
cd codesage && python -m pytest tests/ -q            # 项目根运行
python -m codesage.cli --version                     # 仓库根直接可用
python -m pytest tests/<module>/ -q                   # 单模块(如 tests/permissions/)
python -m pytest tests/<module>/test_x.py -q          # 单文件
python -m pytest tests/...::test_name -q              # 单个测试
DEEPSEEK_API_KEY=xxx python -m pytest tests/ -q       # 含集成(无 key 自动 skip)
```

无 lint/build 工具(引入需 Ask first)。真实 LLM 调用可用 `CODESAGE_VCR=record|replay`(阶段 02 的 VCR 机制)。

## 架构(大图)

- **`codesage/`** — 生产级 harness,唯一活跃代码。**`backend/` 是旧探索代码,不并入、不改动**
- **`Kode-CLI/`** — 参考实现(TypeScript,只读),对照设计用,永不修改
- **`docs/`** — intent/ideas/specs/modules 四层文档,规格驱动开发

### 核心设计不变量(违反需先改主规格)

1. **内部消息契约**:Anthropic 式 ContentBlock 是全系统唯一消息形状(`ai/types.py`);OpenAI/DeepSeek 差异只在 adapter 边界转换(usage 归一化、reasoning_content → thinking 块)
2. **持久化套路**:JSON 文件 + tmp+rename 原子写(`config/atomic.py`,fsync);会话/审计 append-only JSONL + fsync;损坏行跳过不致命
3. **权限决策链**(`permissions/engine.py`):deny > ask > allow,deny 不可被 yolo 绕过;写保护路径优先于 allow;未知工具默认 ask;每次决策恰好一条审计事件;AGENTS.md 永不参与权限
4. **主循环**(`engine/`):显式 while 迭代,非递归 —— Python 递归深度限制(R1 风险,见 `docs/specs/06-engine.md`);Message 流是唯一信息通道;工具失败转 error tool_result 交模型自愈
5. **工具契约**(`tools/base.py`):扁平对象 + async generator 执行;`needs_permissions()` 自声明;权限判断永远在引擎,不在工具内
6. **模型指针**(`ai/client.py`):main/task/compact/quick → profile → 字面量;辅助请求失败自动回退 main;自管重试尊重 retry-after

### 目录规范(主规格强制,所有阶段遵守)

- 每模块一个包:契约层(`base.py`/`types.py`)+ 实现层(类别子包,每工具/每适配器一文件)+ 入口层(`registry.py`/`client.py`);共享辅助 `_common.py`
- 测试镜像源码:`tests/<module>/test_<file>.py`
- 包级 `__init__.py` 显式导出公共 API

## 工作流约定

- master 永远只收合并;每阶段一个 `feat/0N-xxx` 分支(自 master 切)
- 每阶段交付:代码 + `docs/specs/0N-*.md` + `docs/modules/0N-*.md` + 测试全绿 + 合并 push
- 实现顺序按 `tasks/todo.md`;完成一个阶段勾选一个,合并后同步 `feat/0N-xxx` 分支与 master 一致
- 会话中 ponytail(懒人)模式激活;文档与交流用中文

## Boundaries

- **Always**: 阶段开始先读该阶段规格;测试全绿再合并;文档与代码同 PR
- **Ask first**: 新增依赖;调整阶段顺序或模块边界;改 pyproject 元数据
- **Never**: 直接提交到 master(只能合并);修改 Kode-CLI 或 backend 旧代码;提交 API key/密钥
