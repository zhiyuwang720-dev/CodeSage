# 阶段 01 — 配置系统理解文档

> 分支 `feat/01-config`,规格见 `docs/specs/01-config.md`。

## 模块职责

配置系统是全部后续阶段的地基:它回答「这个 harness 的状态放在哪、怎么覆盖」。后续每个模块(LLM 模型指针、权限规则、hooks 配置、MCP 服务器、记忆)都从这里取配置。

## 设计:双轨配置

参考 Kode 的设计,CodeSage 把配置分成两条互不干扰的轨道:

| 轨道 | 位置 | 内容 | 谁消费 |
|---|---|---|---|
| **settings 三层** | `~/.codesage/settings.json`(user)<br>`.codesage/settings.json`(project)<br>`.codesage/settings.local.json`(local) | 行为类:权限规则、hooks、MCP 服务器 | 权限(05)、hooks(09)、MCP(15) |
| **全局配置** | `~/.codesage/config.json` | 产品类:模型 profile/指针、主题、按项目路径的条目 | LLM(02)、CLI(07) |

**为什么双轨?** Kode 的教训:行为配置(权限/hooks)需要三级覆盖(local 覆盖 project 覆盖 user,比如「这个项目本地临时放行某命令」),而产品配置(模型指针)是机器级单份。混在一起会导致"为了改一个项目的权限规则,把模型配置也重写一遍"。

## 关键设计决策

### 1. 三层 settings 的合并语义:dict 递归,list 追加去重

- dict 递归合并(user 有 `permissions: {mode: plan}`,project 有 `permissions: {allow: [...]}` → 两者都保留)
- list 追加去重(user 在前,后面层追加;dict 元素按 JSON 序列化去重)

为什么 list 追加而不是覆盖:user 层通常声明「你机器上通用的允许列表」,project 层追加项目特定的 —— 覆盖会静默丢失 user 的意图。如果未来需要"项目层撤销 user 层的某条",通过 deny 规则表达(权限阶段会看到 deny > allow 的决策链,撤销由 deny 承担)。

### 2. 损坏文件降级,绝不崩溃

`read_json_lossy` / `GlobalConfig.load` 对缺失、损坏、类型不对的 JSON 一律降级为默认值。**一个坏掉的配置文件不能阻止 harness 启动** —— 这是 CLI 工具的存活底线。代价:配置错误静默 —— 权衡后接受(阶段 07 的 CLI 可在启动时打印警告,现在没有 UI 层)。

### 3. 原子写:tmp + fsync + os.replace

任何写入都先写到同目录的 `.name.*.tmp`,fsync 后 `os.replace` 原子替换。**读者永远看不到半写状态**。这是整个 harness 持久化的统一套路(Kode 设计笔记 #14:JSON 文件 + 原子写 + 文件锁),settings 是第一个消费者,后续会话(04)、记忆(17)直接复用 `codesage.config.atomic_write`。

### 4. AGENTS.md 发现:纯文件系统,不跑 git 命令

`find_git_root` 用向上找 `.git` 目录/文件的方式定位仓库根,不用 `git rev-parse` —— 好处:任何检出环境都可用、测试不需要 mock 子进程。**本阶段只做路径发现**(从 git root 到 cwd 每层,`AGENTS.override.md` 替换该层的 `AGENTS.md`),内容读取与注入在阶段 08。

### 5. mtime 缓存

`SettingsStore` 按三个文件的最新 mtime 缓存合并结果 —— 避免每次工具调用都重读磁盘(hooks 阶段会放大这个问题,Kode 同样用 mtime 缓存)。

## 与 Kode 的对照

| CodeSage | Kode | 差异 |
|---|---|---|
| `settings.json` 三层 | 同(settings 文件 user/project/local) | 无 |
| `~/.codesage/config.json` | `~/.kode.json` | 无 |
| 合并:dict 递归 + list 追加 | lodash merge(对象递归、数组整体替换) | **有意不同**:Kode 的数组替换语义会导致 project 层覆盖 user 层的 allow 列表;CodeSage 选追加去重,更符合「三层叠加」直觉。若阶段 05 发现需要替换语义,可加 `replace` 字段 |
| `.git` 向上查找 | `git rev-parse`(实际实现) | CodeSage 不依赖 git 可用性 |
| 无 `.claude/` 兼容 | legacyClaude 只读迁移 | CodeSage 全新开始,无兼容包袱 |

## 已知简化(ponytail)

- 无文件锁:settings 只有读取者 + 单一写入路径,原子写已够;会话/记忆阶段(04/17)多进程写入时才需要锁
- 环境变量只覆盖路径类(`CODESAGE_CONFIG_DIR`/`CODESAGE_CWD`),字段级 env 覆盖等有需要再加
- `GlobalConfig.projects` 的 key 未做路径归一化(大小写),Windows 上同一项目两种写法会建两个条目 —— 阶段 04 引入统一路径工具时一并处理

## 完成标准(对照规格)

- [x] 三层覆盖优先级与深合并行为(单测:test_settings.py 8 项)
- [x] 全局配置原子读写、损坏降级(test_global_config.py 6 项)
- [x] AGENTS.md 发现有序 + override 优先(test_agents_md.py 7 项)
- [x] 原子写工具(test_atomic.py 4 项)
- [x] 全量 25 项单测绿

## 阶段衔接

- 阶段 02(ai):`GlobalConfig.model_profiles/model_pointers` 就位,直接消费
- 阶段 05(权限):`Settings.permissions` 字段就位;规则解析在权限阶段
- 阶段 09(hooks):`Settings.hooks` 就位;执行器在 hooks 阶段
- 阶段 08(context):`agents_md.get_project_instruction_files` 的返回值做内容读取与截断

## 生产级强化(2026-08-05)

三轮修复(对照 Kode 审查,测试 170 → 337):

**修复内容**(批次 1 config + 批次 3 CF1/CF2):
- [高] JSON 读取 BOM 容错(`utf-8-sig`)—— Windows 手改配置(记事本保存)不再整文件静默失效
- [中] atomic_write 强化:symlink 目标解析 + 保留原 mode + 保存失败降级不崩溃
- [中] `save_approval` 并入 atomic_write(统一原子写入口,批次 3 CF1)
- [低] atomic_write 在 Windows EEXIST/EPERM 时重试(批次 3 CF2)
- [中] AGENTS.md 发现:无 git 根时回退读 cwd,不再直接失败

**文件级判定**:
- A 类(已实现):CF1(并入 atomic_write)、CF2(Windows 重试)两项全落地
- B 类(映射阶段 X):无直接命中 —— 01 的地基职责已闭合,消费方在 hooks(09)/记忆(17)
- C 类(理由):legacyClaude/.claude 兼容(全新开始无包袱)、oauth 登录(产品域,不做)

**现状**:及格 → 良好。三处高/中优先项全部落地,Windows 下手改配置与符号链接场景不再破坏数据;文件锁仍留给多进程场景(12/17)。

## 设计决策剖析

### 为什么这么设计

1. **双轨配置(settings 三层 vs 全局单文件)**:行为配置(权限/hooks)需要 user/project/local 三级覆盖("本机临时放行某命令"是真实场景);产品配置(模型指针/主题)是机器级单份。混在一起会"为改一个项目的权限规则重写整个模型配置"—— Kode 的教训。
2. **dict 递归 + list 追加去重**:三层是叠加语义不是替换语义 —— project 层的 allow 列表若整体覆盖 user 层,会静默丢掉机器级意图;撤销需求由权限阶段的 deny 规则承担,不靠覆盖表达。
3. **pydantic + extra="allow"**:permissions/hooks/mcp_servers 先声明,消费方命名稳定;未知键保留使旧配置在新版本不丢、新字段在旧版本不崩 —— 配置文件是长期资产,格式演进必须向后兼容。
4. **原子写统一入口(atomic_write)**:任何写入 = 同目录 tmp + fsync + os.replace,读者永远看不到半写状态;settings/会话(04)/记忆(17)全部复用这一个入口,持久化语义全系统一致。
5. **损坏降级,启动永不失败**:配置是"可重建"资产,坏配置降级为默认值 —— 一个坏文件不能阻止 CLI 启动。代价是错误静默,由阶段 07 启动警告补位。

### 设计原则

- **降级不崩溃(degrade, don't crash)**:缺失/损坏/类型错误一律降级默认值
- **单一写入路径**:所有持久化经 atomic_write;单写者假设,文件锁留给多进程
- **契约先行**:消费方字段先声明,实现阶段按名消费
- **层叠覆盖(later overrides earlier)**:TIER_ORDER = user < project < local
- **平台宽容**:BOM 容忍(记事本)、EPERM 重试(AV 扫描)、symlink 保留(chezmoi/stow)
- **零三方依赖**:合并、发现、写入全用标准库

### 优点

- 合并语义可预测:dict 递归 + list 追加去重,三层直觉与行为一致
- mtime 缓存:每次工具调用都查权限配置的高频 load 路径,磁盘读接近零
- 原子写防半写、失败路径清理 tmp 不留垃圾;Windows EPERM 重试避免偶发失败
- symlink 场景替换链接目标而非链接本身,不破坏用户 dotfile 工作流
- 测试友好:CODESAGE_CWD / CODESAGE_CONFIG_DIR 注入路径,无需 mock 子进程

### 为什么不选用别的技术方案

| 备选方案 | 为什么不选 |
|---|---|
| SQLite | 配置量小、需人类可手改、可版本化对比;JSON + 原子写 + 降级已满足全部语义,零依赖 |
| toml / yaml | 需三方解析器;JSON 是标准库,且与 Kode 配置格式同族 |
| 单层单文件覆盖 | 无法表达"本机临时放行"等局部覆盖;Kode 已验证三层必要性 |
| 文件锁(fcntl/msvcrt) | 单写者假设下锁是死重;多进程写入(阶段 12/17)再加 |
| 内容 hash 缓存 | 每次读盘算 hash,不如一次 stat 的 mtime 廉价;mtime 误判代价只是多读一次 |
| git rev-parse 找仓库根 | 依赖 git 可用性、测试要 mock 子进程;纯文件系统向上找 .git 处处可用 |

## 面试问题整理

### 技术点清单

原子写(tmp+fsync+os.replace)、深合并(dict 递归 + list 追加去重)、mtime 缓存、损坏降级(fail-open)、extra="allow" 配置演进、Windows 平台细节(BOM/EPERM/symlink)

### 面试问题与答案

**Q: 写配置文件为什么用 tmp + fsync + os.replace,不能直接 open 写入?**
**A:** 直接写,读者(另一个进程、编辑器)可能读到半写状态。tmp+rename 让替换成为原子操作:读者只看到完整旧文件或完整新文件。fsync 把数据刷到磁盘(断电不丢);os.replace 在同一文件系统内原子完成;任何失败路径先清理 tmp 再抛错,不留垃圾。
**深度衍生: Windows 上 os.replace 会失败,怎么办?** → 捕获 PermissionError(编辑器/AV 扫描短暂锁文件),unlink 目标后重试一次。另两个细节:mkstemp 默认 0600 会改掉原文件权限 → 写前 stat 保存 mode、替换后 chmod 还原;symlink 先 realpath 解析 → 替换的是链接目标而非链接本身。
**广度衍生: 数据库 WAL、Git object 写入也是这个套路吗?** → 是,同一"先写新、再切换"范式:Git 先写对象再 rename;WAL 顺序追加到日志再刷主存储,崩溃靠日志重放。区别:WAL 是顺序追加(低成本、可回放),配置是整体替换(读多写少、文件小)。

**Q: 三层 settings 合并为什么 dict 递归、list 追加去重,而不是整体覆盖?**
**A:** 三层是叠加语义:user 声明机器通用规则,project 追加项目规则,local 追加本机临时规则。list 整体覆盖会让 project 层静默丢掉 user 层意图(如通用 allow 列表);dict 递归让不同 key 的规则共存。追加去重防膨胀:dict 元素用 json.dumps(sort_keys=True) 作判重 marker,非 dict 用 identity。
**深度衍生: 判重 marker 为什么用 JSON 序列化?** → dict 不可哈希,不能直接进 set;sort_keys 保证 key 顺序不同、内容相同的 dict 判重一致。前提是值可 JSON 序列化 —— settings 本身就是 JSON,约束自然成立;hooks(dict 列表)同样受益。
**广度衍生: 与 CSS 级联/环境变量覆盖相比有何取舍?** → 同为"多层来源 + 确定性优先级"。这里固定 TIER_ORDER、无 !important 式打断;撤销显式走 deny 规则而非覆盖 —— 优先级模型简单到可预测。环境变量只有有/无两态,表达不了三层叠加。

**Q: SettingsStore 为什么用 mtime 缓存合并结果?**
**A:** 工具调用路径上 load() 极频繁(阶段 06 每次工具调用都要查权限配置),每次都读盘 + 解析 JSON 是浪费。缓存记录三个文件的 mtime_ns 列表,load 时一次 stat 比较,命中直接返回缓存对象 —— 读多写少的配置场景命中率接近 100%。
**深度衍生: 为什么三个文件整体比较,而不是只比最新的?** → 任一文件变化都必须失效。只比"最新 mtime"有漏洞:git checkout 可能把某文件改回旧 mtime;整体列表按位相等才命中,语义严格。纳秒精度避免同秒修改漏判。
**广度衍生: 与 HTTP 缓存(ETag/If-Modified-Since)对比?** → 同思路:用轻量变更标记避免重负载。ETag 是内容 hash(强校验),mtime 是弱校验(touch 不改内容也失效)。这里失效代价只是多读一次文件,弱校验可接受;HTTP 失效要重新传输,所以倾向强校验 —— 校验强度跟随失效成本。

**Q: 配置文件损坏时为什么降级为默认值而不是报错退出?**
**A:** 配置不是数据:损坏时空配置是可用起点,启动失败则整个 CLI 不可用 —— 一个坏配置不能阻止 harness 启动是存活底线。read_json_lossy 捕获 OSError/ValueError(缺失/IO/语法),pydantic ValidationError 兜底类型错误,全部降级默认。此策略安全的前提:空配置 = 无规则 = 保守行为。
**深度衍生: 降级后用户怎么感知?** → 目前静默(文档已注明权衡),阶段 07 的 CLI 启动警告补位。注意降级粒度:settings 是三层各自降级空 dict 再合并;global config 是整文件降级默认对象。
**广度衍生: 什么时候应该 fail-closed 而不是 fail-open?** → 涉及安全/完整性时。权限文件损坏若降级为"空表"而空表语义是"全允许",就必须 fail-closed(SSH 拒绝连接同理)。判断标准是"降级后的默认行为是否安全" —— 空 dict 语义保守,所以这里 fail-open 成立。
