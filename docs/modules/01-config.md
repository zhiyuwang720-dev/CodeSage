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
