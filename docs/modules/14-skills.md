# 阶段 14:skills 技能系统(理解文档)

> 权威设计:`docs/specs/14-skills.md`(实现时逐字执行)。本文是设计摘要 + 决策记录 + 实现期关键裁决(S1-S7 交付,1240 测试全绿,2026-08-18)。

## 设计摘要

技能 = 将验证有效的 prompt 模板化的 Markdown 文件(SKILL.md:frontmatter + 正文提示词,「AI Shell 脚本」)。14 在 CodeSage 落地 CC 技能系统的完整语义:定义/加载/提示词管道/双路径调用/权限联动/列表注入/压缩恢复。

- **技能定义(S1,§3)**:`SkillDefinition` 冻结数据类,16 个白名单字段(CC 生态键名:连字符键 `allowed-tools`/`argument-hint`/…,`when_to_use` 例外用下划线);frontmatter 解析器从 `agents/loader.py` **提取**为共享模块 `core/frontmatter.py`(13 §15「utils 共享」兑现,行为保持重构,13 loader 测试逐位绿 = 提取闸门)。
- **发现与加载(S2,§4)**:目录形式 `{dir}/skills/{name}/SKILL.md`(散落单文件忽略);realpath 去重(同文件双入口只保留首次出现);lru 缓存(目录 + name/mtime/size/digest 快照,sha256 防同尺寸编辑);懒执行(注册表只暴露 frontmatter,body 调用时才用 = 发现与执行分离)。三层优先级 **项目 > 用户 > 内置**。
- **内建技能(S2,§4.4)**:`register_bundled_skill` 进程内单例 API,内容 Python 字符串内嵌(CC SIMPLIFY_PROMPT 同款);14 交付演示技能 `simplify`(只读,`context: fork`)—— 同时验证 bundled 层机制 + listing「bundled 永不截断」+ fork 端到端。
- **提示词管道(S3,§5)**:四阶段流水线 —— 基础目录前缀 → 参数替换(`$ARGUMENTS`/`$file`/`$0..$n`/`$ARGUMENTS[n]`,无占位符自动追加)→ 环境变量替换(`${CODESAGE_SKILL_DIR}` 反斜杠转正斜杠/`${CODESAGE_SESSION_ID}`)→ 内联 Shell 执行(`` ```! `` 代码块 + `` !`cmd` `` 行内双模式,并行执行,逐条权限检查,deny → 整次失败,函数式替换防 `$&` 注入,输出超限落盘复用 03 阈值)。
- **双路径调用(S5/S6,§6)**:用户路径 = REPL `/name args` → `find_command` 未命中 → 技能兜底(内置命令恒优先,aliases 参与)→ inline 解析提示词作为下一轮 user 消息复用 `run_single_turn`;模型路径 = SkillTool(`needs_permissions` 动态 SAFE 判定,不进 SYSTEM_TOOLS,`is_concurrency_safe=False`),inline 返回解析后提示词 + metadata 授权,引擎工具结果回收处 `grant_skill_tools` 累积。fork 技能(`context: fork`)经 `skills/fork.py` 复用 13 SubagentRunner 隔离子代理执行。
- **权限联动(S4,§7)**:引擎 `evaluate_tool_use` 增可选参数 `skill_allowed_tools` + 决策链**第 8.5 步**(plan/yolo/REQUIRES_EXPLICIT_APPROVAL 之后、默认 ask 之前)—— 技能授予只豁免「无规则无地板时的默认 ask」,deny/ask 规则、写保护、工作目录、敏感路径、显式批准、plan 全部在前(前置约束全胜);SAFE 白名单(§7.3)走既有 self-declared 路径零引擎改动复刻 CC SAFE_SKILL_PROPERTIES;授权会话内累积(`grant_skill_tools`),审计恰一条(source=skill-allowed-tools)。
- **列表注入(S5,§9)**:`availableSkills` 作为 ContextBundle 段(08 预留扩展位),归 `_render_reminder` **fixed** 类恒保留;三阶段预算算法(全量尝试 → 分区截断 bundled 保留 → names-only 极端模式),8KB 默认预算 / 单条 250 截断 / bundled 永不截断。
- **压缩恢复(S7,§10)**:`skills/state.py` 进程内 invoked_skills 注册表(键 = (agent_id, name),按 agentId 隔离);inline 双路径触发点记录解析后提示词;`AgentLoopConfig.skill_restore` 回调在压缩完成处把恢复段并入既有 `_recovery_reminder` 一次性注入(08/10 机制,零新通道);单技能 5K tokens / 总 25K 预算,最近优先;fork 完成清理。

## 设计决策记录(spec 核心裁决)

1. **技能 = 目录 + SKILL.md(§2 裁决 1)** — 仅支持目录形式,目录承载资源文件与 `${CODESAGE_SKILL_DIR}` 语义;不接受散落单文件。
2. **发现与执行分离(裁决 2)** — 注册表只暴露 frontmatter 轻量字段,正文懒加载;模型上下文付的是元数据不是内容。
3. **allowed_tools 是最弱授权(裁决 3/§7.1)** — 只豁免「无规则无地板时的默认 ask」;引擎可选参数默认 None = 决策链逐位行为零变化(红线 §7.2)。
4. **SAFE 白名单走既有自声明机制(裁决 4/§7.3)** — `SkillTool.needs_permissions` 对纯安全属性技能返回 False → 引擎第 7 步 self-declared 自动 allow;model/effort/context(fork)/agent 视为不安全(与 CC 刻意分歧,对齐「默认拒绝」哲学)。
5. **双路径同汇一处(裁决 5)** — 斜杠与 SkillTool 都构造同一「解析后提示词」:斜杠 = 下一轮 user 消息;SkillTool = tool_result 交模型同轮执行。行为不双轨。
6. **列表会话内静态(裁决 6/§9)** — lru 缓存 + 会话装配一次,无增量重注入(15 MCP 时再上 delta)。
7. **压缩恢复按 agentId 隔离(裁决 7/§10)** — 键 = (agentId, name),fork 完成清理(13 清理位同款)。
8. **技能 hooks 与 agent hooks 同命运(裁决 8)** — 解析存储(SkillDefinition.hooks),执行体 19(09 §11 口径)。

## 实现期关键裁决(S1-S7,review 驱动落地)

1. **S1 frontmatter 提取 = 纯搬移** — 解析逻辑零改动,仅把字段集参数化(`list_fields`/`map_fields`);13 loader 测试逐位绿 = 提取闸门(41 测试)。
2. **S2 realpath 去重测试的 Windows 语义** — junction(`mklink /J`)同真实文件双入口,去重后只产生**一个**技能(名称取 frontmatter name,skill_dir 取首次出现目录);无 junction 权限环境 `pytest.skip`。
3. **S2 subset 恒保留 bundled** — 子代理可见性收窄只收窄非内置层(bundled 可发现性优先哲学同源,§4.4);未知名静默跳过。
4. **S2 paths 过滤 = gitignore 语义按仓库根相对路径** — 前导 `/` = 锚定根(Windows 路径无关);无斜杠模式额外按 basename 匹配任意深度;复用 05 `path_rule_matches`。
5. **S3 行内 shell 正则的 Python lookbehind 定宽约束** — CC 的 `(?<=^|\s)` 在 Python 需定宽 → `(?:^|(?<=\s))` 组合断言;廉价预检 `"!`" in text` 先短路。
6. **S3 shell 块函数式替换** — `re.sub` 的 `$&`/`$'` 特殊序列是注入面 → 从后往前切片拼接(位置不漂移,输出原样插入)。
7. **S4 第 8.5 步位置** — 在 REQUIRES_EXPLICIT_APPROVAL 与 yolo 之后、默认 ask 之前;走到此处即无早返回 → 授权只升级默认 ask;Bash 的显式批准不被技能授权豁免。
8. **S4 敏感路径源被写保护掩盖** — `is_sensitive ⊆ is_write_protected`(静态集),敏感 Read 在第 4 步写保护地板即拦(source=write-protection);矩阵断言「保护地板胜」语义而非具体 source。
9. **S5 SkillTool 授权落点** — `_execute_tools` 返回后读 `item.result.metadata["skill_allowed_tools"]` → `grant_skill_tools`(会话内累积);request_permission 测试必须 async(await True 报错)。
10. **S5 装配层挂 `loop._skills`** — repl_loop 的 `skills` 可选参数缺省回退读取该属性(不侵入 build_loop 返回签名)。
11. **S6 fork 技能 name 恒非 None** — `skill.agent or "general-purpose"`(绝不为 None → 避免 13 的 forkContext 继承语义;fork 技能在**全新**子代理执行 = CC executeForkedSkill)。
12. **S6 子代理 Skill 收窄替换** — 定义声明 `skills` 时把子池 SkillTool 换成 `registry.subset(definition.skills)` 新实例;未声明继承父(SkillTool 契约同工具名)。
13. **S7 skill_restore 并入 _recovery_reminder** — 压缩完成处 f-string 合并(非覆盖);回调缺省 None 零变化;恢复段按 `loop._agent_name` 隔离键。
14. **行尾一致性(全部 S)**:repo 少数文件(loop.py/engine.py/repl.py/assemble.py 等)HEAD 为 CRLF 异常 blob,编辑工具写 LF 会造成暂存整文件翻转 —— 提交前核对 `git show HEAD --numstat` 断言只含真实内容变更(实测每次净插入与实现一致)。

## 红线固化

| 红线 | 锚点 | 状态 |
|---|---|---|
| 决策链 deny>ask>allow 零变化 | `evaluate_tool_use` 无参调用结果与 13 逐位一致(默认 None 回归断言) | ✓ 无参数回归测试 |
| 全部改动可选参数/默认值 | `skill_allowed_tools=None` / `skill_restore=None` / `grant_skill_tools` 空操作 | ✓ 默认路径回归 |
| 13 agents 行为保持 | frontmatter 提取纯搬移 + loader 测试逐位绿;SubagentRequest.allowed_tools 默认 None;子代理默认继承父 SkillTool | ✓ 13 全量回归 |
| 08 context additive | availableSkills 段追加;`_render_reminder` 10 段上限与保留策略不变(归 fixed 类) | ✓ 装配断言 |
| 10 compact 零新通道 | 恢复段并入既有 `_recovery_reminder`;回调为空零变化 | ✓ 压缩恢复端到端 |
| 09 hooks 只存储 | 技能 hooks 解析存储不注册事件;EVENTS 元组不动 | ✓ 无 hooks 改动 |
| 斜杠命令 COMMANDS 零改动 | 技能兜底在 find_command 之后;内置命令恒优先 | ✓ test_slash |
| 工具契约 | SkillTool = 扁平 Tool + needs_permissions 自声明;权限判断永远在引擎 | ✓ 契约声明 |
| 持久化 | invoked_skills 进程内存;reminder 不落盘语义保持 | ✓ state 测试 |
| 零新依赖 / 不修改 backend、Kode-CLI | frontmatter 复用 4.1 提取;gitignore 匹配复用 05 | ✓ |

## 交付与验证

- **S1**:frontmatter 提取(core/frontmatter.py)+ SkillDefinition + 白名单 —— 13 loader 逐位绿 + types 单测(41 测试)
- **S2**:loader(目录发现/去重/lru/懒执行)+ registry(三层合并/subset/paths 过滤)+ listing 三阶段预算 + bundled/simplify —— 全绿(bundled 永不截断断言)
- **S3**:prompt 管道(替换矩阵/env/base 前缀)+ shell(双模式/并行/权限/防注入/截断)—— 替换矩阵 + 权限矩阵单测绿
- **S4**:引擎第 8.5 步 + loop grant 接线 —— 权限联动矩阵绿 + 13 权限矩阵回归绿
- **S5**:SkillTool(inline + 授权落点)+ repl 斜杠兜底 + assemble 装配(availableSkills 段)—— test_skill_tool/test_slash 绿
- **S6**:fork 技能(SubagentRunner 复用)+ agent skills 字段 + 子代理 Skill 收窄 + ASYNC 白名单补 Skill —— fork 端到端绿(含 simplify 演示)+ 13 全量回归绿
- **S7**:state 注册表(隔离/排序/预算)+ skill_restore 接线 —— test_state 绿 + 压缩恢复端到端绿
- **S8**:本文档 + 主规格同步(路线图 14 行修订 + `skills/` 注释「15 技能」→「14 技能」)+ todo 勾选 + 全量回归 **1240 passed, 9 skipped**(2026-08-18)+ 合并 master
