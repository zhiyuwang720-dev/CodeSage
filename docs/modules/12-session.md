# 阶段 12:会话生命周期(树状会话)(理解文档)

> 权威设计:`docs/specs/12-session.md`(实现时逐字执行)。本文是设计摘要 + 决策记录 + 实现期关键裁决(S1-S5 全部交付,1008 测试全绿,2026-08-14)。

## 设计摘要

会话 JSONL 从「纯消息行」升级为「typed-entry 行」:七类 entry(message/lane/bookmark/branch_summary/operation/model_change/meta)写同一文件,**消息带 parent 链**,**lane 指针表达分支**(单文件多分支,对齐 Pi),旧格式文件惰性兼容读取。双视图:`load()` = 活跃 lane 线性视图(引擎消费面零改动);`build_tree()` = 树视图(CLI 导航)。

- **七类 entry 契约**(§3.2):每行自包含(uuid/timestamp/parent/data);`message` 是唯一进入 LLM 上下文的 entry,其余六类是**应用状态**,只被读取器消费 —— 应用状态与模型上下文的物理分离(PI-10)。
- **lane 指针推进(写死设计,§3.4)**:`append_message` 写消息后**顺带追加同名校验 lane 指针 entry**(leaf = 新 uuid);`fork` = 追加新 lane entry(leaf = 分支起点 entry 自身)+ 重置游标;**活跃 lane = 文件最后一条合法 lane entry**(坏行跳过,全失败 main 兜底)。fork 不创建新文件、不复制消息 —— 零拷贝,历史共享。
- **操作日志(§7,单向)**:引擎工具轮在权限闸通过后、真实执行前追加 `operation`(kind="tool_started", args_summary 截断 200);`find_open_operations` 纯函数检测「末尾 operation 或其后只有应用状态 entry」= 未完成;`--continue` 命中时打印三段式提示,只提示不重放。
- **会话自描述(§8)**:新建会话首行 `meta`(model/show_thinking/cwd/system_prompt_hash/session_id);模型切换追加 `model_change`;首条有意义 user prompt 提取标题 → 第二个 meta entry(title ≤80,后者胜合并)。
- **恢复(§4.5)**:compact 落盘 `branch_summary` 快照(leaf = 切点后第一条消息 uuid,不改消息链);`--resume` 沿目标 lane 找最近摘要(跨 lane 过滤:leaf ∉ 链则跳过),注入摘要 + leaf 前 2 条 user 为上下文起点(10 的 boundary 消息模式复用);无摘要回退 07 旧逻辑。
- **归档(§9)**:归档 = 移动文件(os.replace 同盘原子,root 级 `sessions/archive/` + project 级 `sessions/{project}/archive/`);`list_sessions` 排除任何层级 archive/(语义微调红线);`/archive --restore` 一行恢复。
- **CLI 升级(§4.4/§5/§6/§9.2)**:`--lane` 选分支;五斜杠命令 `/tree /fork /bookmark /sessions /archive`;渲染遵循 §1.4 UX(符号即语义、80 字符截断仅显示层、圈号序号、20 行翻页、entryId 上下文窗口前 5 后 3)。

## 设计决策记录(spec 核心裁决)

1. **单文件 typed-entry + lane 指针(用户指定)** — 否决 Kode 多文件方案:所有分支保存在单个文件中,append-only 不变;「可共享的历史记录」。
2. **fork = 追加 lane 指针(对齐 Pi session.ts:338-351)** — 分支/回滚天然;`{scope: "tree"}` 整树复制不做(单文件已含全树,复制无意义);从任何先前位置继续 = 线性视图换 lane,零新代码路径。
3. **--resume 与 fork 分工** — resume = 压缩/摘要后新会话文件(轻量省 context,07 语义);fork = 原地分支共享历史;--continue 居中(同文件追加)。三语义并存不混淆。
4. **操作日志单向(只记 tool_started)** — 配对 end 要侵入工具轮全部出口(成功/失败/abort/钩子阻断),收益低;中断恢复只需「最后一个工具在干什么」;误报只产生提示无副作用(R6)。
5. **应用状态与消息物理分离(PI-10 部分采纳)** — 不动 SessionMessage;load() 线性视图永远只投影 message 链。
6. **branch_summary 是落盘快照,非内存权威** — 压缩仍在内存态(boundary 消息流不变);摘要 entry 供跨进程恢复;恢复时跨 lane 过滤防误用。
7. **list_sessions 排除 archive/(§9.1 语义微调红线)** — 归档会话从活跃枚举消失,04 枚举用例回归;归档不删除(--restore)。

## 实现期关键裁决(S1-S5,review 驱动落地)

8. **S1 P1:_active_lane 缺字段容错** — `entry.data.get("name"/"leaf")` 任一 None → continue 退回上一个合法 lane(语义损坏行跳过,§3.4/R4 兜底);混合 lane 测试固化。
9. **S3 P1:model_change 去重** — meta.model 是首行快照永不更新,重复 --continue 同模型会重复追加污染历史;装配时比较对象 = **最后一条 model_change 的 `to`**(无则回退 meta.model),from_ 同取该值,`current not in (None, model)` 才追加。
10. **S4 P1:resume 注入按 leaf 定位** — 「摘要前 2 条 user」按整链末尾取是错的(压缩在链中发生时 leaf 不是最新消息);`chain[:idx]` 取 leaf 之前最近 2 条 user。
11. **S5 P2:归档/恢复目标存在即拒绝** — os.replace 无条件覆盖违背「永不删除」精神;`dest.exists()` 守卫(一行)。
12. **S5 P2:entryId 解析限 message/operation** — meta/lane/bookmark 的 uuid 不是可导航 entry,命中后报 "entry not found" 误导;uuid 分支限类型。
13. **标题提取落点 = engine/loop.py `_persist`** — 全部消息唯一落盘漏斗;`_title_written` 门闩 + 续写会话读 meta 判据(每会话至多一次文件读);工具结果载体/reminder 跳过。
14. **numbered_entries = message + operation** — 文件序 1-based 编号,`/tree` 行与 `--continue` 中断提示(entry 序号)共用;圈号 ①-⑳ 渲染(>20 阿拉伯数字)。
15. **/sessions id 截断取尾部时间戳段** — 本项目 id 为 `session-YYYYMMDD-HHMMSS-ffffff` 前缀形态,前 4 位恒 "sess" 零区分度;取尾部 11 字符(含微秒,同秒可辨)+ `…` 前缀;§1.4.2/§9.2 规格措辞已同步。
16. **/tree 数字二义性:页优先** — 数字 ≤ 总页数 = 页码,否则 = entry 编号;entry 恒可用 uuid 触达上下文窗口(docstring 显式声明)。
17. **/tree 组合筛选语义(已知取舍)** — `--type` 与 `--bookmarks` 是 OR 且 ref 存在时筛选被静默忽略(reviewer P2#3,无测试覆盖);UX 边角,待后续迭代。

## 红线固化

| 红线 | 锚点 | 状态 |
|---|---|---|
| SessionMessage 零改动 | `core/messages.py` | ✓ 实证 `git diff HEAD` 为空 |
| 04 会话测试零改动 | `tests/core/test_session.py` | ✓ 零改动,逐轮回归 |
| Session 签名/append/load 返回语义 | `core/session/session.py` | ✓ 仅新增方法/属性 |
| 旧格式读取逐字节一致 | §3.3 | ✓ legacy 文件两态测试 |
| list_sessions 排除 archive/(微调) | §9.1 | ✓ 整分量相等匹配,不误伤 |

## §1.4 交互规格自检(四项)

1. **提示语三段式** ✓ — 注意类提示(`--continue` 中断恢复)`[!]` 前缀 + 事实(entry 序号)+ 动作建议三要素齐备;完成类(fork/bookmark/sessions)平铺;`test_resume_inject.py` 结构断言(有/无中断两态,断 `[!]` 前缀 + `entry N` 序号 + ` —— ` 分隔符,不逐字比对文案)。
2. **信息密度分层** ✓ — `/sessions` 一行一会话(id 截断段);`/tree` 行 ≤80 字符;截断仅显示层(测试双向断言:渲染行 ≤80 且 `session.load()[0].content` 全量相等)。
3. **符号可读(去色仍可辨)** ✓ — `/tree` 渲染零 ANSI 色码,`→`/`✓`/`!` 纯符号承载语义;测试显式断言 `"\x1b[" not in out`。
4. **即时反馈** ✓ — `build_tree` 纯函数 O(n)(page 截断兜底,无缓存);fork/bookmark 追加写无回读;全量渲染目标 <10ms 达成。

## 风险与边界(R1-R11,spec §12)

| # | 风险 | 缓解(实现期状态) |
|---|---|---|
| R1 | 04 存储格式变更红线 | 本 spec 即 Ask;§3.3 惰性兼容;test_session.py 零改动硬闸门 ✓ |
| R2 | 树状复杂度失控 | §1.2 裁剪:只渲染+筛选+书签+fork 四件套;无树编辑/无交互选择器 ✓ |
| R3 | load() 语义漂移 | §4.3 单一出口:load() ≡ linear_messages(entries, active_lane);引擎 E2E 断言 ✓ |
| R4 | lane 指针损坏 | 读端容错:坏行跳过退上一合法 lane;全失败 main 兜底 ✓(S1 P1 加固) |
| R5 | branch_summary 双份摘要漂移/跨 lane 误用 | 落盘快照非权威;跨 lane 过滤(leaf ∉ 链跳过)✓ |
| R6 | 操作日志单向假未完成 | 误报只提示不重放,无副作用;配对留 13 ✓ |
| R7 | fork 后 --continue 语义混淆 | --continue 默认活跃 lane(--lane 显式选);/tree 可视化 + 提示语含 lane 名 ✓ |
| R8 | 归档破坏 id 枚举 | list_sessions 排除 archive/ 显式红线;--restore 可逆 ✓ |
| R9 | 单文件体积 | 压缩快照 + 归档共同缓解;树渲染按页截断 ✓ |
| R10 | 多进程并发写(13 场景) | §3.5 格式预留;12 单写者不实现锁(13 兑现) |
| R11 | 交互文案被测试固化 | 固化的是格式与符号,文案仍是可改字符串;符号即语义让换肤不触碰测试 ✓ |

## 与前后阶段衔接

- **依赖**:04(消息/会话契约)、07(cli resume/commands)、10(压缩摘要管线,boundary 消息复用)—— 全部已就绪,零新依赖。
- **12 → 13(子代理)**:fork API(§4.2)成为 13 forkContext 的存储基座(传 `session_id + lane name` 即完全定位历史);`step_attempt` operation kind 预留;操作日志配对完整语义 13 再评(R6);多进程文件锁(R10)13 兑现。
