# 阶段 20:最小改动 CodingAgent(战略转向)

> 阶段 19 之后不再按「复刻 Claude Code」的路线继续,而是转向项目真正的价值锚点:
> **一个能理解复杂项目的 CodingAgent** —— 启动陌生代码库时自动建立代码智能引擎(codebase-memory-mcp
> 知识图谱),通过 ponytail 的「懒人资深工程师」阶梯 + 图谱的影响面分析,强制 agent 用**最小 diff** 完成任务。
>
> 本文是转向的权威地图。现有阶段 01-19 是坚固底座(权限/审计/子代理/技能/MCP/上下文工程),
> 本阶段在其上长出差异化能力层。

## 0. 战略背景与价值定位

### 0.1 为什么转向

阶段 01-19 逐步复刻了 Claude Code 的完整 harness(配置/模型/工具/权限/引擎/CLI/上下文/钩子/
压缩/任务/会话/子代理/技能/MCP),质量扎实、测试全绿。但**独立使用价值低**——Claude Code 本身
免费且成熟,「另一个复刻」没有差异化意义。

真正的价值锚点(用户重新定义):
1. **代码库智能引擎自动启动**:陌生库 → 自动索引 → 知识图谱可用,agent 不靠反复 grep/read 摸结构
2. **最小改动执行**:基于图谱影响面分析,让 agent 用最小 diff 完成任务(而非大改)
3. **ponytail 融合**:借 ponytail 的「懒人资深工程师」阶梯,把最小改动从「提示词要求」变成「引擎行为」

差异化点 = Claude Code 的通用定位没覆盖的**垂直场景**:针对复杂项目、以最小代码改动为目标。

### 0.2 对齐 DeepSeek Harness / Cordis 的启示

DeepSeek Harness(dsh)基于 Cordis 内核:**一切皆插件**——模型/工具/技能/会话/沙箱/存储/循环/UI
都由插件提供,内核只负责加载/卸载/依赖,配置层自由组合。核心概念:
- **seam**(可替换能力)= Service Definition(接口)+ Service Provider(实现)+ Consumer(消费者)三件套
- 注册是可逆副作用(插件卸载时撤销)
- 能力经 `ctx` 服务与类型化事件协作

阶段 19 规划的「插件化注册层」正是为此。本阶段 20 的差异化能力(CodeIntelligenceService、
影响面约束层)将按 **seam 思想**设计,为 19 插件化铺路,而非再造单体内核。

## 1. 目标与范围

### 1.1 做什么(20 主要做什么)

1. **CodeIntelligenceService**(`intel/service.py`):把 codebase-memory-mcp 从「可选的 MCP 服务器」
   提升为「核心服务」——启动自动索引当前代码库,暴露影响面查询接口(架构/调用链/变更影响),
   供引擎约束层与 agent 上下文消费。
2. **引擎级影响面约束层**(`intel/minimal_change.py`):在引擎 `_permission_check` 之外的独立关卡,
   基于图谱影响面分析,产出「改动最小集」约束,引导/拦截工具调用到最小侵入路径(重,真正差异化)。
3. **ponytail 融合**(`intel/ponytail.py`):把 ponytail 的「删优于加/复用优先/根因修复」阶梯作为
   常驻系统提示 + 技能可用,并编码进引擎的改动约束(最小改动成为引擎行为)。
4. **端到端验证**:陌生库自动索引 → 影响面分析 → ponytail 约束 → 最小 diff 完成任务;
   对比有无约束层的 diff 差异,证明价值。

### 1.2 不做什么(候选裁剪)

| 候选 | 裁决 | 理由 |
|---|---|---|
| 重构既有 15 个模块为 seam | **裁**(19) | 20 聚焦差异化能力层;seam 重构是 19 插件化的任务,20 只按 seam 思想设计新能力 |
| 实现完整前端/UI | **裁** | 20 用 CLI/引擎级能力验证价值,UI 后续 |
| 支持多仓库索引 | **裁** | 20 先单库(当前项目)验证链路 |
| codebase-memory 全 15 工具桥接 | **裁** | 20 只接核心:index/list_projects/search_graph/trace_path/detect_changes/get_architecture |

### 1.3 边界(与 15/14/05/06 的划分)

- **与 15(MCP)**:codebase-memory 仍是 MCP 服务器(15 已接入);20 新增 `intel/` 服务层在其上封装
  「自动索引 + 影响面查询」,是 MCP 能力的**产品化**,不改 15 协议层
- **与 14(技能)**:ponytail 通过 14 技能系统接入(register_bundled_skill);20 额外把其阶梯编码进引擎约束
- **与 05(权限)**:影响面约束层**不改变权限决策链**(deny>ask>allow 零改动);约束是「改动引导」,
  不替代权限;引擎 `_permission_check` 保持原样,约束层作为独立关卡叠加
- **与 06(引擎)**:约束层在 `_permission_check` 之外作为独立检查点(§4);不重排既有决策流

## 2. 核心裁决

1. **CodeIntelligenceService 是一等公民服务**(非可选 MCP):启动自动索引,常驻图谱访问,
   agent 上下文含「库结构概要 + 影响面入口」。默认开启(可 `CODESAGE_NO_INTEL` 关闭)。
2. **最小改动 = 引擎级约束,非提示词要求**:约束层在引擎拦截/引导工具调用到最小侵入路径,
   与权限引擎独立共存;这才是真正的差异化价值(而非靠 LLM 自觉遵循提示词)。
3. **影响面分析先于改动**:任何写操作(Edit/Write)前,约束层先查图谱「谁调用/谁依赖目标」,
   产出改动最小集,引导 agent 只改必要的调用路径。
4. **ponytail 阶梯 = 引擎行为**:把「删优于加/复用优先/根因修复」编码进约束层的改动建议;
   不只是注入规则文本。
5. **seam 思想设计新能力**:CodeIntelligenceService/影响面约束层按 Service(接口)+Provider+Consumer
   设计,为 19 插件化铺路。
6. **先单库验证,再考虑多库/UI**。

## 3. CodeIntelligenceService(`intel/service.py`)

```python
class CodeIntelligenceService:
    """代码智能引擎服务(spec 20 §3)。

    封装 codebase-memory-mcp,提供「自动索引 + 影响面查询」。启动时索引当前项目,
    暴露查询接口供引擎约束层与 agent 上下文消费。codebase-memory 为 MCP 服务器
    (15 已接入),本服务在其上做产品化封装。
    """

    def __init__(self, project_dir: Path, cbm_cli: str | None = None) -> None:
        # cbm_cli:codebase-memory-mcp 可执行路径(自动发现或配置)
        self._project = project_dir
        self._project_key: str | None = None
        self._indexed = False

    async def ensure_indexed(self) -> None:
        """索引当前项目(幂等):已索引则跳过,未索引则 index_repository。"""

    async def get_architecture(self) -> dict:
        """库结构概要(语言/包/入口/路由/热点),注入 agent 上下文。"""

    async def impact_of_change(self, symbol_or_file: str) -> dict:
        """影响面分析:谁调用/谁依赖目标,返回影响集(供改动最小集计算)。"""

    async def changed_symbols(self, diff: dict) -> dict:
        """detect_changes:未提交改动映射到受影响符号 + 风险分级。"""

    async def trace(self, fn: str, direction: str) -> dict:
        """调用链追踪(入站/出站)。"""
```

- **自动索引**:`ensure_indexed` 在装配层(build_loop)调用,幂等;失败降级(不阻塞启动,
  `CODESAGE_NO_INTEL` 可整体关闭)
- **project_key**:codebase-memory 索引后返回的 project 名(如 `E-Mac-CodeSage-codesage`),
  所有查询需带
- **接口薄封装**:用 subprocess 调 `cbm cli <tool> --project <key> ...`,解析 stdout JSON;
  后续可切 MCP 客户端调用(15)

## 4. 引擎级影响面约束层(`intel/minimal_change.py`)

### 4.1 接入点

在引擎 `_permission_check`(loop.py:991)之外,**独立的「改动引导」关卡**。引擎执行工具前
调用约束层,约束层基于图谱给「改动建议」:

```python
async def minimal_change_guard(
    item: ScheduledTool,
    intel: CodeIntelligenceService,
    state: RunState,
) -> ToolResult | None:
    """改动最小集约束:对写操作(Edit/Write),查影响面,引导最小侵入路径。

    返回 None = 放行(不影响权限决策);返回 ToolResult = 拦截并给改动建议。
    不替代权限引擎(05),只做「改动引导」。
    """
    if item.tool.name not in WRITE_TOOLS:
        return None  # 只约束写操作
    impact = await intel.impact_of_change(item.input.get("file_path"))
    # 计算改动最小集,给建议;若改动目标非最小路径,引导 agent
    ...
```

### 4.2 改动最小集算法

对写目标文件,查图谱「谁引用/谁调用」,产出:
- **影响集**:目标改动会波及的符号/文件
- **最小集**:为完成当前任务,真正需要改的调用路径(删冗余、复用既有 helper)
- **建议**:「此改动会影响 X/Y/Z;更小的路径是改共享函数 A(一行),而非每个调用点」等

ponytail 阶梯编码进建议生成(§5):
1. 改动是否需要存在(YAGNI)
2. 库内已有 helper/模式可复用?(改共享函数优于改每个调用点)
3. stdlib/平台能力覆盖?
4. 一行能解决?

### 4.3 与权限/钩子共存

- **不改变权限决策链**:`_permission_check` 原样;约束层是叠加关卡
- **不新增权限审计事件**:约束是改动引导,不产生 permission 审计
- **可禁用**:`CODESAGE_NO_MINIMAL_CHANGE` 关闭约束(仅留建议提示)

## 5. ponytail 融合(`intel/ponytail.py`)

### 5.1 技能接入(14 复用)

ponytail 6 个 skill(ponytail/audit/debt/gain/help/review)经 14 `register_bundled_skill` 接入,
`ponytail` 为主 skill(懒人阶梯:YAGNI/复用/标准库/一行)。ponytail-mcp 暴露的 prompt/tool
作为 MCP 能力(15 接入)。

### 5.2 引擎行为编码(§2 裁决 4)

不只注入规则文本,把阶梯编码进约束层的**改动建议生成**:
- 删除优先于添加
- 复用既有 helper 优先于新建
- 根因修复(改共享函数)优先于逐调用点修补
- 一行优先于五十行

建议格式:`[code] — skipped: [X], add when [Y].`(ponytail 输出契约)

## 6. 端到端验证

### 6.1 demo 场景

启动陌生代码库(可用 CodeSage 自身或一个测试仓库),验证:
1. **自动索引**:启动时 `ensure_indexed` 完成,架构概要注入上下文
2. **影响面分析**:agent 尝试改某函数前,约束层查「谁调用」,给最小集
3. **ponytail 约束**:agent 被引导到最小 diff(复用 helper / 改共享函数 / 一行)
4. **完成任务**:端到端完成一个小任务(如「新增一个错误处理」「改一个公共函数」),
   产出最小 diff

### 6.2 价值对比

同一任务,有无约束层的 **diff 行数 / 改动文件数** 对比:
- 无约束层:LLM 默认可能大改(新建文件、加抽象、逐调用点改)
- 有约束层:被引导到最小侵入路径

## 7. 测试计划

| 文件 | 测试 |
|---|---|
| `tests/intel/test_service.py` | CodeIntelligenceService:ensure_indexed 幂等/架构概要/影响面查询(用真实 codebase-memory 或 mock CLI) |
| `tests/intel/test_minimal_change.py` | 约束层:写操作拦截/读操作放行/影响集计算/最小集建议/禁用开关 |
| `tests/intel/test_ponytail.py` | ponytail skill 接入(14 复用)/阶梯编码进建议生成 |
| `tests/engine/test_minimal_change_integration.py` | 约束层与引擎共存:权限决策链零改动回归/约束叠加 |

## 8. 实施步骤(S1-S5)

| 步 | 内容 | 闸门 |
|---|---|---|
| S1 | intel/service.py(CodeIntelligenceService:自动索引 + 影响面查询)| 真实 codebase-memory 索引本库成功(已验证:3786 节点)|
| S2 | intel/minimal_change.py(引擎级约束层)| 写拦截/读放行/最小集建议单测绿 |
| S3 | intel/ponytail.py(技能接入 + 阶梯编码)| ponytail skill 经 14 接入,阶梯建议单测绿 |
| S4 | 引擎接线(loop 叠加约束层)+ 装配(自动索引)| 约束层与权限共存回归绿 |
| S5 | 端到端 demo + 价值对比 + 文档 | 陌生库最小 diff 完成任务;diff 对比体现差异 |

## 9. 风险与边界

| # | 风险 | 缓解 |
|---|---|---|
| R1 | codebase-memory 索引慢/失败阻塞启动 | ensure_indexed 幂等 + 失败降级 + CODESAGE_NO_INTEL 关闭 |
| R2 | 约束层干扰正常改动(误拦) | 约束只「建议引导」,默认不硬拦;CODESAGE_NO_MINIMAL_CHANGE 关闭 |
| R3 | 约束层破坏权限决策链 | 独立关卡,不重排 _permission_check;权限矩阵回归绿 |
| R4 | 影响面分析不准(图谱不完整) | 保守:约束层给建议,agent 最终决定;图谱可 re-index |
| R5 | 依赖外部二进制 | 15 已定义内置托管注册表;codebase-memory 按需安装,缺则降级 |

## 10. 与路线图的关系

- **依赖**:15(MCP 接入 codebase-memory)、14(ponytail skill 接入)、06(引擎约束叠加点)、05(权限共存)
- **20 → 19 plugins**:CodeIntelligenceService/影响面约束层按 seam 思想设计,19 插件化时挂载为可替换插件
- **主规格留痕**:本阶段转向后,`docs/specs/codesage.md` 路线图追加 20 行,标注战略转向