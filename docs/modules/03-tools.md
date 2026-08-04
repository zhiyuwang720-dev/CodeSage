# 阶段 03 — 工具系统理解文档

> 分支 `feat/03-tools`,规格见 `docs/specs/03-tools.md`。

## 模块职责

工具系统是模型与真实世界的接口。三个阶段模块各管一段:契约与执行(本阶段)、权限门控(阶段 05)、调度并发(阶段 06)。**工具的职责边界:执行 + 自声明,不做权限判断、不做调度。**

## 设计:扁平对象契约

Kode 的工具契约是「元数据 + 展示 + 执行」三合一的扁平对象(`satisfies Tool<In,Out>`),不是类继承树。Python 版对应:

```python
class Tool:                       # 扁平对象:类属性即元数据
    name = "Read"
    description = "Read a text file..."
    input_schema = {...}
    is_concurrency_safe = True    # 只读工具可并行(阶段 06 消费)

    def needs_permissions(self, input) -> bool: ...   # 自声明,阶段 05 消费
    def validate_input(self, input) -> None: ...      # 执行前校验
    async def call(self, input, ctx) -> AsyncIterator[ToolResult]:  # async generator
```

**为什么 async generator 而不是普通 async 函数**:执行可以边跑边 `yield ToolProgress`(阶段 07 的终端实时渲染),最终 `return ToolResult`。阶段 06 的调度队列可以直接消费这个形态,不需要工具重写。

**为什么 `needs_permissions` 是自声明**:Bash 永远需要权限,Read 只在路径越界时需要。权限引擎(阶段 05)读这个声明决定「这次要不要问」—— 询问频率由工具自己声明,决策由权限引擎做。

## 内置工具设计要点

| 工具 | 关键决策 |
|---|---|
| **Read** | 二进制探测(NUL 字节嗅探 8KB)→ 拒绝而非乱码;行号 + offset/limit;编码降级链 utf-8 → gb18030 → latin-1(Windows 中文文件真实需求) |
| **Edit** | **old_string 必须唯一**,歧义时要求 `replace_all=true` —— 防止模型误改多处;找不到时报错并提示「文件可能已变,重新 Read」(对应 Kode 的 hash 陈旧性校验的前置形态,阶段 06 补 readFileTimestamps) |
| **Write** | 复用阶段 01 的 `atomic_write` —— 工具写入同样不允许半写状态 |
| **Glob/Grep** | 统一 `SKIP_DIRS`(.git/node_modules/__pycache__ 等,对应 ripgrep 默认忽略);输出路径统一 posix 分隔符(模型看到 `sub/b.py`,不因平台而异) |
| **Bash** | 见下 |

## Bash 最小安全(本阶段的地板)

阶段 16 会做完整 8 层纵深,本阶段只交付「地板」:

1. **validateInput**:timeout ∈ [0, 600000],空命令拒绝
2. **真超时 + 进程树 kill**:`asyncio.wait_for(proc.communicate())` 超时后 —— POSIX `killpg(SIGKILL)`(start_new_session 建独立进程组),Windows `taskkill /pid X /T /F`(杀整棵树)。**为什么必须杀树**:`sh -c "cmd & wait"` 这类命令超时后,只杀父进程会留下孤儿子进程继续跑(实测:测试里 sleep 孙进程在父被杀后仍会写文件)。Kode 的 BunShell 也是显式处理进程树的。
3. `needs_permissions()` 永远 True —— Bash 是权限引擎的第一个真实用户

## 与 Kode 的对照

| CodeSage | Kode | 差异 |
|---|---|---|
| 7 个内置工具 | 29 个(含 Task/LSP/Web/Notebook 等) | 阶段推进逐步补齐;Task→13、Web→15、LSP 未排期 |
| Grep 用 stdlib re + 遍历 | 打包 ripgrep 二进制 | **有意简化**:性能上限标注在代码里,大仓库搜索慢时换 rg(python 有 `ripgrep` 绑定或打包二进制) |
| 无 presenter(JSX 渲染) | `renderToolUseMessage` 等 | 阶段 07 的 CLI 用简单文本渲染,不需要 JSX |
| 超大结果落盘在阶段 06 | engine 层统一处理 | 同位置,只是时间差 |

## 已知简化(ponytail)

- `_walk_files`/`rglob` 是 O(n) 全量扫描 —— 大仓库下 Glob/Grep 慢,换 ripgrep 时一并处理
- Read 图片转 base64 未做(阶段 07 UI 需要时加)
- Bash 无输出流式 progress(阶段 07 终端要实时输出时,`_run` 改 yield)
- 无 `context_modifier`/`new_messages` 的完整实现(ToolResult 字段已预留,阶段 06 消费)

## 完成标准(对照规格)

- [x] Tool 契约定型,7 个内置工具可用(84 项单测)
- [x] Bash 真超时 + 跨平台进程树 kill(Windows taskkill 实测)
- [x] 二进制/超大文件处理不崩(截断/拒绝)
- [x] 注册表 spec 生成正确(阶段 06 直接可用)
- [x] 84 passed, 1 skipped(Windows 专属断言),全量绿

## 阶段衔接

- 阶段 05(权限):`needs_permissions` 声明 + Bash 永远门控
- 阶段 06(engine):`ToolRegistry.specs()` → 模型工具定义;async generator → ToolUseQueue;超大结果落盘统一做
- 阶段 07(CLI):progress 事件 → 终端渲染
- 阶段 15(MCP):MCP 工具适配成同一个 `Tool` 对象(强制 needs_permissions=True)
