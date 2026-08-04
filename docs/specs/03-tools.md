# Spec: 阶段 03 — 工具契约与内置工具

> 分支:`feat/03-tools`。依据主规格 `docs/specs/codesage.md`(阶段 03)。

## Objective

工具系统是模型与真实世界的接口:Tool 契约(扁平对象三合一)、注册表、第一批内置工具(文件/搜索/Shell)。Bash 带最小安全(真超时/kill + validateInput),完整 8 层纵深在阶段 16。

## 范围

**做**:
1. `Tool` 契约:元数据 + 校验 + 执行三合一扁平对象;执行是 async generator(yield progress, return result);`needs_permissions()` 自声明
2. `ToolRegistry`:注册/查询/生成模型可见 ToolSpec
3. 内置工具:LS、Read、Write、Edit、Glob、Grep、Bash
4. Bash 最小安全:真超时 + 跨平台进程树 kill、validateInput(timeout 范围)

**不做**:权限检查(阶段 05,`needs_permissions` 只是声明);调度队列(阶段 06);超大结果落盘(阶段 06 引擎统一处理);提示词/JSX 渲染(Kode 的 presenter,阶段 07 前不需要);MCP 工具(阶段 15);LLM 意图闸门/沙箱(阶段 16)。

## 项目结构(本阶段新建)

```
codesage/codesage/tools/
  __init__.py
  base.py        # Tool / ToolResult / ToolProgress / ToolUseContext / ToolError
  registry.py    # ToolRegistry + get_builtin_tools
  fs.py          # LS / Read / Write / Edit
  search.py      # Glob / Grep
  shell.py       # Bash(最小安全)
tests/
  test_tool_base.py
  test_registry.py
  test_fs_tools.py
  test_search_tools.py
  test_shell.py
```

## Commands

```bash
pytest tests/ -q
```

## Code Style

主规格风格;工具为扁平对象(`Tool(name=..., call=...)`),非类层级;async generator 执行体。

## Testing Strategy

- 每个工具:正常路径 + 错误路径(不存在文件、二进制、超时)
- Bash:真实 subprocess(echo/超时 kill),Windows 进程树
- 注册表:注册/覆盖/去重/spec 生成

## Boundaries

- **Always**: 工具校验输入(schema 已在契约层);Bash 必须有超时上限;路径解析归一化
- **Ask first**: 新增依赖;改变 Tool 契约
- **Never**: 工具内做权限判断(只声明 `needs_permissions`);Bash 无超时执行

## Success Criteria

- [ ] Tool 契约定型,7 个内置工具全部可用
- [ ] Bash 真超时 + 跨平台进程树 kill 实测(Windows)
- [ ] 二进制/超大文件处理不崩
- [ ] 注册表 spec 生成正确(阶段 06 直接可用)
- [ ] 全量单测绿
