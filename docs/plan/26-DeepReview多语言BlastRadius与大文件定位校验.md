# Plan 26：CodeSageDeep 多语言 Blast Radius 与大文件定位校验

版本：草案 v2；状态：待审。基线：Plan 25 的 Deep Review 链路。本文只规划确定性辅助分析，不增加模型调用，也不改变 Reviewer、Cross 的审查职责。语言范围为 Python、Go、TypeScript/JavaScript、C/C++ 和 Java；“支持”仅指下文列出的保守静态关系，不等于完整依赖分析。

## 0. 背景与已核实的问题

AACR `microsoft__typescript-go@b970689` 的 `checker-and-grammar-handling` Reviewer 生成了两条指向 `internal/checker/checker.go` 的 Finding，位置分别为 29383–29386 和 5083–5087。固定 head `b970689fe86e0b6ef4f5285b18343ddf16b294eb` 中，该文件是模式 `100644` 的普通 blob，大小 1,328,486 字节，共 29,812 行。两处位置均有效。

旧代码在 `agents/reviewer.py::_head_line_count` 中用工具读取上限 `max_file_bytes=1_000_000` 校验完整文件。超过此上限时返回 `None`；`services/reviewer_result_mapper.py` 随后把所有 `None` 解释成 `head_file_unavailable_or_non_regular`。工具单次读取限制与 Finding 的行号校验不是同一约束。这是程序逻辑错误，先前把它归因于模型输出定位错误的结论不成立。

本轮已在工作区做独立的小范围修复（尚未提交）：行号校验使用单独的 `max_head_line_count_bytes=16_000_000`，通过有界 Git 读取器取固定 head blob；缺失计数时使用诚实的 `head_line_count_unavailable` 诊断。Cross 新 Finding 同样区分“计数不可用”和“行号越界”。对失败会话保存的原始 `FinalizeReview` payload 做确定性重放后，两条 Finding 均被接纳，诊断为空。旧 AACR 归档仍是原始 `partial` 结果，不回写历史运行。

另一个问题是 Blast Radius 的名称容易让人误以为它支持所有语言。当前实现只分析 Python 静态 import；C、Go、TypeScript 等代码的审查仍运行，但不会得到依赖影响线索。`related_count=0` 不能解释为“没有跨文件影响”。

## 1. 当前实现与实际数据流

```text
Directory Filter 的 review_paths
  → 没有 .py：返回 []，记录 skip 诊断
  → 有 .py：git ls-tree -r -z <固定 head>，筛选 Python blob
  → git cat-file --batch 读取 Python 源文件
  → Python ast.parse 提取静态 import，建立 importer → imported-file 图
  → 反向查询 review_paths 的一跳直接 importer
  → 排序后的 related_paths
  → Anatomy.related_paths
  → Semantic 与 Planner 提示词
```

当前边界：head tree 最多 16MB，Python 文件最多 2000 个，批量 blob 总计最多 32MB，单文件最多 1MB，Git 工具超时默认 10 秒。超限或读取失败由编排器降级为空线索；不会取消审查。实现只识别可以唯一解析到仓库内 Python 文件的静态 import，不处理动态 import、调用图或传递依赖。种子仅为过滤后的 `review_paths`。

有两个值得一并改的消费问题：

1. `Anatomy.related_paths` 没有独立的输出数量或字节上限，Semantic 和 Planner 都会收到整个路径列表；大型反向依赖集合会浪费 token。
2. 跳过、分析成功但无命中、超限降级都可能表现为 `related_paths=[]`；后续阶段无法判断这三种情形。Evidence 当前没有使用传入的 Blast Radius 列表，Cross 也没有直接消费它，故无需把图或源码送入这两个阶段。

## 2. 目标与边界

- 对审查文件涉及的语言给出有限、可解释的一跳影响线索；它是调查提示，不是影响范围证明。
- 所有语言共享调用接口、状态和输出预算；各语言保留自己的解析规则。不建设全语言统一 AST、符号数据库或启动语言服务器。
- Blast Radius 全程使用确定性代码。扫描失败或不支持某语言时继续审查，并准确记录覆盖状态。
- 不把完整依赖图、文件内容、调用链或大量路径放进模型上下文。Reviewer 仍通过现有只读工具按需查看代码。
- 固定 head、Directory Filter 的秘密路径规则、用户过滤配置、Git 读取时间和内存上限始终生效；扫描辅助文件不扩大 `review_paths`，也不绕过 Finding 的现有发布边界。

## 3. 统一接口，按语言实现

在 `services/blast_radius.py` 保留编排入口，定义很小的内部契约：

```python
@dataclass(frozen=True)
class BlastHint:
    path: str                 # 可能需要检查的固定 head 普通文件
    seed_path: str            # 触发该线索的 review 文件
    relation: str             # static_import / same_package / header_include / test_pair

@dataclass(frozen=True)
class BlastResult:
    hints: tuple[BlastHint, ...]  # 有界保留结果，不是全量图
    observed_hint_count: int     # 适配器发出的原始命中数，可重复；降级时只是下界
    coverage_by_language: dict[str, str]  # analyzed / unsupported / degraded
    truncated: bool
    diagnostics: tuple[str, ...]

class BlastAnalyzer(Protocol):
    languages: frozenset[str]

    async def analyze(self, context: BlastContext, sink: BlastSink) -> None: ...
```

`BlastContext` 只提供固定 head、过滤后的种子路径、共享的最小 head tree 索引、有界 blob 读取能力和全局剩余预算。`BlastSink.add()` 在加入时完成路径合法性检查、原始命中计数和有界候选集内去重/top-K 保留；不维护随总命中数增长的全局去重集合。适配器不能先构造无界列表或完整图再交给 Dispatcher 裁剪。Python 现有完整 `importer → imported-file` 图应改为有界模块映射 + 文件逐个解析并向 Sink 发出种子的一跳命中，而不是仅给旧函数包一层预算。`coverage_by_language` 每种出现的语言恰有一个状态：`analyzed` 表示该适配器的声明规则完整执行，**不**表示真实依赖完整；`unsupported` 表示无适配器；`degraded` 表示部分扫描、超限或失败。一个适配器局部超限不阻止其他语言与主审查；若全局期限耗尽，尚未完成的语言记 `degraded`，不得承诺继续扫描。空 `hints` 因此仍能区分无命中、不支持与未完成。`observed_hint_count` 是原始命中次数，可能含重复；降级时是已观察部分的下界，不可当成全量唯一文件数。

Dispatcher 以固定 head 路径后缀路由；需要同时修正 `diff_engine.detect_language()` 的后缀表，否则 `.pyi`、`.mjs`、`.cjs`、`.h`、`.cc`、`.cxx`、`.hpp`、`.hxx` 等已被 Directory Filter 接纳的文件会在语言识别处漏掉。`.java` 已有语言标识。已进入 `review_paths` 但不能映射到适配器的类型按 `unsupported:<后缀>` 汇总，不能因为 `detect_language()==""` 就完全消失。对于头文件，沿本地 `#include` 关系找直接包含者；不按 `.h` 猜定 C 或 C++ 的编译语义。语言覆盖状态按具体适配器聚合，不把结构性的 `test_pair` 命中误报成该语言依赖分析已完成。

路径准入不能直接复用面向 diff `FileChange` 的 `DirectoryFilter.filter()`。新增小型 `is_related_path_allowed` 判定：路径规范化、固定 head 中存在且模式为 `100644`/`100755`、非 secret、遵守用户排除和默认排除/生成文件规则，并跳过已经属于 `review_paths` 的目标；`include_paths` 沿用现有覆盖规则，不误当全局白名单。固定 head 的非普通文件不得作为种子参与依赖分析。符号链接、submodule、删除路径、二进制源文件和仅存在于工作区的路径均不得成为 hint。`PlanRepair` 继续独立验证模型生成的 `context_files`；Blast 不自动提升文件 owner，也不允许 hint 绕过 Planner 的验证。

种子来自 `review_paths`，因此删除文件被过滤为 context-only，改名的旧路径也不在固定 head；V1 不声称能从旧路径构建反向依赖。记录这类遗漏数量或明确诊断，必要时后续再设计有界的 merge-base 侧分析，不在本计划里暗中扩大扫描范围。

V1 先保持 `Anatomy.related_paths: list[str]` 的业务兼容性，由 `BlastResult.hints` 投影成有界路径列表。`DeepReviewRunContext` 另存 `BlastResult`；Planner 的输入构建显式接收它的简短状态/线索投影，不能只从 `Anatomy.related_paths` 取路径，否则语言状态仍丢失。`PreparationReport`/`ProcessReport` 增加带默认值的简短 Blast 观测字段（状态、原始命中数、展示数、扫描量与裁剪原因），保留原 `related_paths` 兼容展示；历史报告没有该字段时仍可反序列化。该过程观测不进入最终 `DeepReviewResult` 的业务内容哈希。字段不得误称为“全量唯一相关文件数”。不要为此建立数据库表或扩充运行时协议。适配器的预期降级在 Dispatcher 内转成结果；未预期异常也应隔离记录，只有取消信号继续向上抛，避免当前编排器把普通适配器异常升级成整个 Preparation 失败。

## 4. 语言覆盖顺序

| 范围 | 确定性规则 | 不声称覆盖的情况 |
|---|---|---|
| Python | 移入适配器，保留现有 AST 静态 import 与一跳反向依赖 | 动态 import、反射、运行时注入 |
| Go | 以 `go.mod` 的 module 路径和目录为单位解析静态 import；同 package 文件标记 `same_package`，包间关系标记 `static_import` | 运行时注册、反射；无法解析的多 module/replace 不猜测 |
| TypeScript / JavaScript | 固定 head 中唯一可解析的相对 `import`、`export ... from` 和静态 `require`；按已存在文件解析扩展名及 `index` | 动态 import、构建器别名；复杂 `tsconfig paths` 暂记未解析 |
| C / C++ | 唯一可解析的本仓库引号 `#include`，由头文件追到直接包含者 | 宏生成 include、外部 `-I`、条件编译的实际分支 |
| Java | 轻量词法扫描忽略注释和字符串，读取 `package`、顶层类型声明、显式类型 `import p.Type` 与显式静态 `import static p.Type.member` / `p.Type.*`；只有固定 head 中包名、文件名、顶层类型名相符且 FQCN 唯一时建立一跳反向引用 | `import p.*`、`import module ...`、同 package 隐式使用、嵌套/其他包私有类型跨文件映射、`module-info.java`、`package-info.java`、反射、注解生成代码、重复 FQCN、多 source root 歧义；不执行 Maven/Gradle |
| 其他语言 | 可选的严格同名测试文件配对，标为 `test_pair`；其余记 `unsupported` | 不把目录相近推断成真实依赖 |

Go 与 C 是现有 TypeScript-Go、LVGL AACR 用例直接暴露的缺口；TS/JS 与 Java 同批加入，以覆盖常见多语言仓库。Java 的静态 import 仅用于找到拥有该类型的唯一源文件，**不**证明方法调用或运行时影响；静态通配符可解析拥有类型，但普通类型通配符不能定位一个被使用的文件（见 [Java Language Specification §7.5](https://docs.oracle.com/en/java/javase/26/docs/specs/jls/jls-7.html)）。Rust、Kotlin、C# 等随后按真实基准样本增加小适配器，不以一个宽松正则宣称全语言支持。每个适配器要输出可定位的关系类型；有歧义时不产生边，并记录未解析数量。`same_package` 是 Go 的结构关联，不是已证实的调用依赖；`test_pair` 也始终标为弱线索。

## 5. 时间、空间、缓存与输出预算

### 5.1 扫描

先按种子文件语言选择适配器，没有适配器时不扫描源码。26.1 即建立最小的固定 head `HeadTreeIndex`（保留预算内的候选源文件路径、模式、blob OID，并限制原始 tree 输出字节数、保留条目数和索引内存），供 Blast Radius 与 Planner 复用；不能等到 26.3 才交付，因为 `BlastContext` 在 26.1 已依赖它。现有 `run_planner_agent` 在调用 Harness **之前**先独立执行全量 `ls-tree`，超过上限就失败。26.1 要改变这个时序：先运行 Harness 得到有界 `ReviewPlanDraft`，再汇总其中有限的 `context_files`，索引命中者直接校验；不在索引中的候选对固定 head 做有界批量精确路径查询，最后把校验通过的路径集合交给 `repair_plan`。配置、文档等非源文件也可能是合法 context。若 tree 超限或不可用，Blast Radius 单独降级，Planner 仍采用该逐路径校验。精确查询必须将用户路径作为 **literal pathspec**（并使用 `--` 隔开选项），核对返回路径、普通文件模式、secret 规则，并受路径数、超时和 Git 输出预算限制；查询失败时拒绝该 context path，不接受未经验证的路径。不能因为只收源文件的索引缺少某路径就判定其不存在。

只批量读取启用适配器所需的文件类型。Blast 扫描的单文件上限必须独立于 `file_read` 的 1MB 展示上限；建议初始 `max_blast_source_file_bytes=4_000_000`、总 blob 上限 32MB，确保 TypeScript-Go 的 1.3MB `checker.go` 不被悄悄跳过。继续保留文件数和整体时间上限；建议 Blast Radius 总期限 10 秒。tree 建立、blob 读取和解析都计入此期限；多语言按固定顺序轮转有界批次，避免一种语言一次性占完共享预算。先检查 OID/blob 大小再请求下一批，不一次性请求所有文件再在结果中丢弃大文件；二进制/NUL 或无法安全解码的源文件跳过并计数，不从替换字符中解析关系。任何预算截断均产生 `degraded` 和已扫描/跳过数量；不得把部分扫描称作零命中。具体 Git 子进程使用全局剩余时间。批量 Git 输出在到达保留上限后仍须排空管道并正确终止子进程，避免 Windows 上的阻塞。

现有 `max_import_*` 属于 Python 实现的配置名；新增通用 Blast 限额时要明确兼容映射，保留旧配置可解析，避免 `extra="forbid"` 使既有配置启动失败。Directory Filter 的 `max_file_bytes` 当前按 **diff 字节数**排除变更，与 Blob 扫描限额和 Finding 行号校验限额分属不同用途，不能混用。

### 5.2 缓存

第一步只做一次 run 内的 head tree 与解析结果缓存。原始语法提取可按 `(blob OID, parser version)` 缓存，但“import 指向哪个仓库文件”的解析还依赖当前路径、head tree、模块/source-root 配置与过滤配置；不得只按 blob OID 缓存已解析边。AACR 每个 case 单独启动进程，run 内缓存不能加速重复跑同一 case。跨进程持久缓存只在基准计时证明收益后设计；若实施，解析图键至少包含固定 head tree OID、适配器版本和模块/过滤配置摘要，受容量约束，原子写入并失效，不保存源码。

### 5.3 给模型的内容

内部先在有界 `BlastSink` 的种子/目录桶中去重并保留少量最佳线索，最后按关系强度、固定种子顺序和路径稳定选取。Sink 最多保留 **48 条候选**，目录桶数也固定有界；实现不能为每个新种子无限增加桶，桶选择须使用稳定路径摘要而非进程随机 hash。Blast 早于 Anatomy，故不能在此处使用尚未生成的 `changed cluster`；公平性只按种子路径的确定性目录桶实现，不依赖模型或后续阶段。优先直接静态引用，其次同 package，最后同名测试配对。最终给 Planner 的整个 Blast 区块（包括覆盖状态、字段名和序列化开销）最多 **12 个路径、1,500 UTF-8 字节**；一个路径放不下就整条跳过，绝不截断路径或写半个 JSON。覆盖状态也必须压缩成有界摘要，详细诊断留在运行报告，避免状态文本独自突破字节预算。多条线索指向同一路径时只展示一条最强关系；`observed_hint_count` 允许重复，展示数则是唯一保留路径数。`truncated` 包括扫描降级和展示裁剪，并明确原因。报告/事件记录原始命中数、展示数和状态，不保存完整图。只传 `path`、简短 `relation` 和覆盖状态，不传源码或长解释。

Semantic 当前收到 `related_paths`，但它只能看有界 diff，依赖路径不能帮助它验证行为；从 Semantic 输入去掉该列表。Planner 接收上述简短提示，并明确“这是可选调查线索；为空不表示无风险，也不证明风险；以固定 head 代码为准”。Reviewer 不直接接收 Blast 图，只有 Planner 自行选入、且经 `PlanRepair` 校验的 `context_files` 会成为建议读取对象。Cross 和 Evidence 保持不直接消费 Blast 数据。`Anatomy.related_paths` 只得到有界投影，不持有全部线索。

## 6. 实施包与影响文件

| 包 | 内容 | 主要文件 |
|---|---|---|
| 26.0 工作区已实施、未提交 | 分离行号校验与工具读取上限；大文件真实 payload 确定性重放 | `agents/reviewer.py`、`schemas/config.py`、`services/reviewer_result_mapper.py`、`services/cross_repair.py`、`tests/deep_review/test_reviewer.py` |
| 26.1 接口和预算 | `BlastResult`/有界 Sink/适配器契约、语言状态、12 路径/1,500 字节上限；最小 `HeadTreeIndex` 与 Planner 精确路径查询兜底；现有 Python 实现迁入；Semantic/Planner 输入调整；路径准入、过程报告与配置兼容 | `services/blast_radius.py`、`services/blast_analyzers/python.py`、`services/head_tree.py`、`services/directory_filter.py`、`services/git_runner.py`、`schemas/config.py`、`schemas/output.py`、`agents/semantic.py`、`agents/planner.py`、`services/orchestrator.py`、对应测试 |
| 26.2 常用语言 | Go、TS/JS、C/C++、Java 的保守静态规则与同名测试配对；补齐语言后缀路由 | `services/blast_analyzers/` 下按语言分文件、`services/diff_engine.py`；小型固定 Git 仓库夹具 |
| 26.3 性能复验 | 测量多语言扫描时间/内存、分批读取、固定 head 解析缓存和 Planner 复用效果；只有测出跨进程重复扫描瓶颈后才另案加入磁盘缓存 | `services/head_tree.py`、`services/git_runner.py`、性能测试；不把 26.1 必需的索引推迟到本包 |

整个计划不修改 `.ai()` / `.harness()` 调用次数、Reviewer/Cross 业务 Schema、AACR 算法或审查工具对固定 head 的读取语义。Planner 仅调整本地校验时序，不新增模型轮次。运行事件只增加分析语言、状态、扫描文件/字节数、耗时、原始命中数/输出路径数；不写源码。

## 7. 测试与验收

1. 大文件定位：普通 head blob 超过 `max_file_bytes` 但低于独立行数上限时，合法行号可被 Reviewer 和 Cross 接纳；真正越界、删除、符号链接、二进制、超限或读取失败各有诚实诊断。已保存的 TypeScript-Go 两条 Draft 在零模型调用重放中应再次被接纳。
2. Python 现有一跳 import、相对 import、唯一后缀别名测试继续通过。`.pyi`、`.mjs`、`.cjs`、C/C++ 头文件和 `.java` 由正确路由接纳；未知语言和不支持的动态关系不产出伪边，覆盖状态不写成“无影响”。
3. Go package、TS/JS 相对 import、C/C++ 本地 include、Java 显式类型与静态 import 各用小型固定 Git 仓库验证：直接依赖命中、歧义路径不命中、固定 head 与工作区内容不同仍使用 head；1–4MB 的正常源文件不得因为工具展示上限被跳过。Java 的普通通配符 import、module import、重复 FQCN、多 source root、注释/字符串伪 import、`module-info.java` 均不得产生假边；无需 Maven/Gradle。
4. 删除与改名旧路径只报告 V1 覆盖缺口；secret、用户排除、默认排除/生成文件、符号链接、submodule、二进制及其他未通过 related-path 准入的文件不泄漏到提示词。已通过准入但不在 `review_paths` 的普通 head 文件正是可展示的 related hint。`include_paths` 与现有 Directory Filter 一致，Blast hint 不越过 Finding 发布边界。
5. 混合语言 PR 中一个适配器局部超时或超限，尚有全局预算的其他适配器和主审查照常完成；全局期限或树索引超限时未完成语言为 `degraded`、计数注明下界，Planner 的有界精确查询兜底仍可校验 context path。Windows 下大 Git 输出不会卡住。计划 Draft 生成后才精确校验 `context_files`，且不额外调用模型。
6. 即使有数千条静态关系，Sink 常驻保留量不超过 48 条，模型输入中 **完整序列化 Blast 区块**不超过 12 路径和 1,500 UTF-8 字节；没有半截路径、源码、完整图或重复列表。相同 head 和配置重复运行的排序与内容哈希稳定；同一 blob 在不同 head 模块布局下不复用错误解析边。旧 `PreparationReport`/`ProcessReport` 无新增字段时仍可读取，新增观测不改变最终业务内容哈希。
7. 用 LVGL C 与 TypeScript-Go Go 样本执行 `--through anatomy`，记录各语言状态、线索和耗时；再比较 Planner 输入字节数。增加 Java 固定仓库的 `--through anatomy` 无模型验收。无需为 Blast Radius 验收重新调用付费模型。

完成 26.1–26.3 后，才根据基准中的真实漏报和扫描耗时决定下一批语言适配器与是否增加持久缓存。
