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

## 生产级强化(2026-08-05)

三轮修复(对照 Kode 审查,测试 170 → 337):

**修复内容**(批次 1 tools + 批次 3 T1/T2/T3):
- [高] Edit/Write read-first + mtime/sha256 陈旧性校验(`ensure_read_freshness`/`record_read`)—— 外部改动被静默覆盖的路径封死
- [高] Bash destructive guard(rm -rf 临界目标/空操作数拒绝)+ cd 限制(工作目录外拒绝)
- [高] run_in_background + TaskOutput/TaskStop(BackgroundTaskStore,输出落盘独立于单次调用)
- [高] Read 0.25MB 输出上限(截断提示分页);图片/PDF 读取未实现(保持 ponytail 简化)
- [高] ToolUseContext 补 abort_event/readFileTimestamps/readFileHashes;validate_input 接进执行链
- [中] contextModifier 修复(cd 后 cwd 错位)
- [高] Grep rg 化:rg 子进程快路径 + 纯 Python 兜底,输出格式一致(T1)
- [中] TodoWrite:会话级 todo 列表,幂等替换(T2)
- [中] WebFetch:SSRF 防护 —— 私网/环回/link-local/元数据地址全拒,裸主机名拒绝(T3)

**文件级判定**:
- A 类(已实现):T1/T2/T3 全落地,内置工具 7 → 10
- B 类(映射阶段 X):Bash 纵深(16)、MCP(15)、技能/斜杠(14)
- C 类(理由):Ink UI 渲染(~300 文件)、LSP(未排期)、checkpoints

**现状**:及格(偏下) → 良好。三类高危静默破坏路径(Edit/Write 陈旧、Bash 破坏性命令、无后台任务)全部封死,搜索性能由 rg 兜底;Read 图片/PDF 明确不做,LSP 未排期。

## 设计决策剖析

### 为什么这么设计

1. **扁平对象契约,不是类继承树**:Kode 的 Tool 是"元数据 + 展示 + 执行"三合一扁平对象。类属性即元数据(name/description/input_schema),一文件一工具,注册表按 name 查表即可;几十个互不相关的工具之间没有可复用行为,继承树的层级抽象无收益。
2. **执行 = async generator**:边跑边 yield ToolProgress(阶段 07 终端实时渲染),最终 return ToolResult —— 一个契约同时服务流式 UI 与最终结果。基类 call() 包装 _run(),简单工具只实现 _run。
3. **needs_permissions 自声明,决策在引擎**:询问频率由工具声明(Read 只读不常问、Bash 永远问),"要不要问、问完给不给"由权限引擎决定 —— 权限策略集中在引擎,工具永远不做权限判断。
4. **read-first + mtime/sha256 陈旧校验**:模型基于 Read 快照编辑;外部改动被静默覆盖是最高危数据丢失路径,Edit/Write 强制"先 Read + 新鲜度校验",自己写入后刷新基线。
5. **Bash 真超时 + 进程树 kill**:`sh -c "cmd & wait"` 超时只杀父进程会留下孤儿继续跑(实测复现:孙进程在父被杀后仍写文件),必须独立进程组 + 杀整树。
6. **rg 快路径 + Python 兜底**:搜索性能由 rg 子进程承担(30s 超时),缺失/失败自动回退纯 Python,两条路径输出格式完全一致。

### 设计原则

- **最小特权**:只读工具自声明不常问;Bash 永远门控
- **fail-closed**:Edit 歧义拒绝、rm -rf 空操作数/保护路径拒绝、SSRF 目标拒绝 —— 不确定就不做
- **信任边界校验**:validate_input 执行前校验,工具输入永不可信
- **数据安全优先**:原子写、陈旧校验、输出截断(保护模型上下文)
- **契约先行**:input_schema 即模型可见契约,spec() 生成
- **平台一致性**:输出统一 posix 分隔符、kill 双平台实现

### 优点

- 工具与引擎解耦:权限/调度/渲染都不在工具内,工具可独立单测
- 三类高危静默破坏路径全封死:Edit/Write 陈旧覆盖、Bash 破坏性命令、无后台任务
- 双轨搜索:快路径性能 + 兜底路径零依赖,行为一致
- 输出上限(Read 250KB / Bash 30K / WebFetch 50KB)防止模型上下文被撑爆

### 为什么不选用别的技术方案

| 备选方案 | 为什么不选 |
|---|---|
| 类继承树(Tool → FileTool → ReadTool) | 无行为复用收益;扁平对象注册/序列化/测试更简单 |
| 工具内做权限判断 | 权限规则散落无法统一决策链;引擎集中 + 工具自声明是最小耦合 |
| 打包 ripgrep 二进制 | 跨平台分发体积成本;rg 子进程零打包,Python 兜底保可用性 |
| 纯 Python 搜索 | O(文件×行) 全量扫描,大仓库慢;rg 快 1-2 个数量级 |
| 同步 subprocess.run 执行 Bash | 阻塞事件循环,无法并发调度;asyncio 子进程 + communicate |
| 直接写文件(无原子写) | 复用 config.atomic_write,半写状态不存在 |

### 技术点清单

read-first 陈旧性校验(mtime+sha256)、进程树 kill(超时/中止/取消三路径)、async generator 执行契约、rg 快路径 + Python 兜底、SSRF 防护、破坏性命令静态守卫、后台任务(output 落盘独立于单次调用)

## 面试问题整理

### 面试问题与答案

**Q: Edit 为什么要求"先 Read 再编辑",还要校验文件新鲜度?**
**A:** 模型基于 Read 的快照做修改。Read 之后文件被外部改动(用户手改、其他进程),按旧快照替换会静默覆盖外部修改 —— 数据丢失。机制:Read 时在 ctx 记录 mtime_ns + sha256;Edit/Write 前 ensure_read_freshness:mtime 相同直接放行;mtime 变化再比对 hash,内容相同(touch)刷新基线,不同则拒绝并提示重新 Read;自己写入成功记录新基线,避免自我冲突。
**深度衍生: 为什么 mtime 变了还要二次 hash?** → mtime 是廉价弱信号:git checkout 恢复、touch 会让 mtime 变而内容不变,直接拒绝会逼模型白读一遍。hash 是强校验:相同则无害刷新基线继续,不同才拒绝。两级代价:mtime 预筛(一次 stat)、hash 确认(一次读盘),只在确实可疑时付出后者。
**广度衍生: 与数据库乐观锁有何对应?** → 同一模式:读取时取版本戳,写入前验证,冲突拒绝而非覆盖 —— 相当于 UPDATE ... WHERE version=? 或 Git 的 index 检查。区别:版本由"最近一次 Read"持有在会话内存、不持久化;单进程会话内足够,多客户端需持久化版本(阶段 12 议题)。

**Q: Bash 超时为什么必须杀进程树,只杀父进程会怎样?**
**A:** shell 命令会派生子进程(`sh -c "cmd & wait"` 典型),只杀父进程,孙进程变孤儿继续运行 —— 后台服务存活、测试里孙进程继续写文件,都是实测复现。因此 POSIX 用 start_new_session 建独立进程组 + killpg(SIGKILL);Windows 用 taskkill /pid /T /F 杀整树。超时、abort_event、外层 CancelledError 三条退出路径全部"先杀树、再 wait",不留僵尸。
**深度衍生: kill 本身失败怎么办?** → 双平台兜底:taskkill 抛异常或超时(10s)、killpg 抛 ProcessLookupError(进程已退)时退化为 proc.kill()。杀完再 wait 确保回收,竞态窗口内进程自行退出也安全。
**广度衍生: 与容器世界的孤儿进程处理有何关系?** → POSIX 孤儿进程被 init(PID 1)收养继续跑,系统不会替你清理 —— "主动杀树"是必须的。容器里 PID 1(或 tini)负责回收;k8s 的 process namespace 共享、preStop hook 都是"让整树生命周期受控"的工程化。

**Q: Grep 为什么设计成 rg 子进程 + Python 兜底双轨?**
**A:** 纯 Python 是 O(文件数×行数) 全量扫描,大仓库慢(代码注明性能上限);rg 是 Rust 实现、并行 + SIMD,快一到两个数量级。打包 rg 有跨平台分发成本,所以:shutil.which 探测到 rg 走快路径(30s 超时,失败/超时自动兜底),否则回退纯 Python;两条路径输出格式完全一致(rel:lineno: content),模型无感知。
**深度衍生: rg 默认尊重 .gitignore,这里为什么 --no-ignore --hidden?** → harness 语义是"搜用户看到的文件"而非"git 跟踪的文件",所以显式关闭 ignore,再用与 Python 路径同一份 SKIP_DIRS(.git/node_modules/__pycache__ 等)排除噪音目录 —— 排除集合一致,两条路径行为才能一致。
**广度衍生: 快路径/慢路径模式还常见于哪里?** → CDN 回源、内存缓存落盘、JIT,都是"优路 + 语义等价的兜底路"。设计要点:兜底路必须能探测快路径缺席(shutil.which),且两条路输出契约一致 —— 否则模型看到的搜索结果随环境变化。

**Q: WebFetch 的 SSRF 防护为什么在工具内做?怎么做的?**
**A:** 工具是网络请求的信任边界,防护必须在发出任何字节前:scheme 限 http/https;拒绝内嵌凭据(user:pass@);拒绝裸主机名(无点且非 localhost);getaddrinfo 解析出的每个 IP 必须公网 —— 私网/环回/link-local/保留/多播/未指定全拒(10/8、172.16/12、192.168/16、127/8、169.254/16、::1、fc00::/7,IPv4-mapped 继承 IPv4 判定);不跟随重定向(目标可能指向内网,返回 Location 由用户决定)。
**深度衍生: 检查的 IP 与实际连接的 IP 会不一致吗?** → 会:httpx 连接时会重新 DNS 解析,DNS rebinding 可让检查后解析结果变化。代码以 ponytail 注明:把连接钉在已检查的 IP 上是升级路径。当前防护已覆盖"直接指内网"的主要攻击面,威胁模型内够用。
**广度衍生: SSRF 绕过通常从哪里来?** → 解析器不一致:应用按一种方式解析 URL、请求库按另一种(0.0.0.0、十进制/八进制 IP、IPv4-mapped、重定向)。这里统一 ipaddress 库 + 枚举非法类,正是消除解析器分歧;重定向不跟随是第二个关键决策 —— 很多绕过发生在跳转之后。
