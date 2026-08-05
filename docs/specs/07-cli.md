# Spec: 阶段 07 — CLI REPL 与 V1 闭环验收

> 分支:`feat/07-cli`。依据主规格 `docs/specs/codesage.md`(阶段 07)。
> **V1 主线最后一站:闭环验收在此。**

## Objective

终端前端:交互 REPL + 单次模式(非交互,验收自动化用)。消费 AgentLoop 的 SessionMessage 流,权限 ask 决策接到终端,信号接到 abort。**V1 验收**:真实 API 端到端完成「创建文件」类任务,权限三态生效,审计有记录。

## 范围

**做**:
1. `cli` 包:装配(build_loop)、单次模式(run_once)、交互 REPL
2. 渲染:assistant 文本、thinking(默认摘要,`--show-thinking` 全显)、工具结果摘要
3. 权限询问:y/n/remember(记住 → save_approval)
4. 信号:SIGINT → 第一按中断循环,第二按退出
5. `/mode`、`/help`、`/quit` 斜杠命令(最小集)
6. 会话持久化(~/.codesage/sessions/,时间戳 id)
7. **V1 验收**:真实 API 单次模式创建文件 + deny 规则拒绝 + 审计断言

**不做**:富 TUI/ANSI 花哨(纯文本);AGENTS.md 上下文(08);流式逐字渲染(07 先整条输出,08/10 后可升级);斜杠命令系统(15);会话 resume/fork(12)。

## 项目结构(本阶段新建)

```
codesage/codesage/cli/
  __init__.py          # main() 入口
  assemble.py          # build_loop: 装配全部依赖
  render.py            # SessionMessage 流 → 终端文本
  repl.py              # 交互循环 + 单次模式
  permission_prompt.py # 权限询问(y/n/remember)
  base_prompt.py       # 系统提示骨架
codesage/cli/__main__.py  # python -m codesage.cli
tests/cli/
  test_render.py
  test_repl.py         # 单次模式(mock LLM)
  test_v1_acceptance.py # 真实 API 验收(无 key skip)
```

## Commands

```bash
python -m codesage.cli "创建 demo/hello.md"        # 单次模式
python -m codesage.cli                            # 交互 REPL
python -m codesage.cli --mode yolo "..."          # 权限模式
DEEPSEEK_API_KEY=xxx python -m pytest tests/cli/test_v1_acceptance.py -q  # 验收
```

## Testing Strategy

- render/repl:mock LLM 全离线
- **V1 验收**(真实 API,integration 标记):
  1. `--mode yolo` 创建文件 → 文件存在
  2. `deny Write` 规则下 → 模型收到拒绝,文件不存在
  3. 审计 JSONL 存在且有事件
- 权限询问:模拟输入 y/n/r

## Boundaries

- **Always**: 单次模式无 UI → ask 决策拒绝(安全默认);渲染不泄漏工具输入内容(只摘要)
- **Ask first**: 引 UI 框架依赖(rich 等)
- **Never**: 交互模式下把 ask 自动放行;打印 API key

## Success Criteria(V1 验收清单)

- [ ] 单次模式:真实 API 完成文件创建任务,产物存在
- [ ] deny 规则:任务不执行,模型收到拒绝
- [ ] 审计记录存在
- [ ] 交互 REPL 可用(手动冒烟)
- [ ] 全量单测绿
