# 阶段 21 规格:插件内核(Python 版 Cordis)

> 分支:`feat/21-plugin-kernel` · 前置:阶段 20(已交付)· 参考:DSH `docs/cordis-primer.md` + `vendor/cordis/`(只读)+ cordis-mini(只读,思路参考)
> 战略背景:见 `docs/specs/后续设计想法.md`「V2 插件化转向」;改造路线图见 `docs/specs/22-pluginization.md`。

## 1. Context(为什么做)

CodeSage 从 V1 到阶段 20 已经按「契约层 + 组合根」建成(assemble.py = 构造器 DI 组合根),
但**组合方式仍硬编码**:换模型/换工具/换权限策略都要改 Python 代码。V2 的目标是 DSH 式
「一切皆插件」:能力 = 插件,通过声明式组合装配,配置层可替换。

Cordis 本体(TS)2709 行、loader+include 另 1530 行,无法从 Python 直接复用;cordis-mini
(600 行纯 Python)已验证五概念可完整落地。本阶段交付 **纯 stdlib 的 Python 版内核**,
为阶段 22-25 全面改造提供组合层。**不引 cordis-py**(第三方移植版 = 新依赖 + API 绑定,
自研成本更低且是学习目标本身)。

## 2. Objective

交付 `codesage/kernel/`(约 1200~1900 行 + 单测),实现 Cordis 五概念 + Loader + Patch:

1. **插件即服务**:Service 子类或 `apply(ctx)` 函数,生命周期挂载到上下文
2. **上下文即服务仓库**:`ctx.<key>` 稳定键注册/查找,键查找替代 import
3. **inject 声明依赖**:所需服务未就绪则挂起,就绪后激活(加载顺序由依赖推导)
4. **类型化事件**:emit / waterfall / parallel / serial 四种派发
5. **可逆副作用**:`ctx.effect()` 收集 disposer,插件卸载逆序回滚
6. **Loader + Patch**(阶段 22 Profile/Bundle 的基础):manifest 行装载、按 id patch、last-wins

## 3. 核心概念映射(TS → Python)

| Cordis(TS) | Python 实现 |
|---|---|
| `Service` 子类 + `inject` + `Config` | `Service` 基类:`ctx_key` / `inject: list[str]` / `Config` schema;`apply(ctx)` 函数插件 = 普通 async 函数 |
| `Context` 服务代理(链式父 ctx) | `Context` 类:`__getattr__` 代理到服务仓库,子 ctx 链式回退 |
| `ctx.provide(key, value)` | `ctx.provide(key, value)`(launcher 预置服务,如 configuredAgentIdentities 等价物) |
| `ctx.on/emit/waterfall/parallel/serial` | 同签名;事件词汇表 = dataclass 注册(替代 TS 声明合并) |
| `ctx.effect(execute)` 逆序回滚 | `ctx.effect(execute) -> disposer`,fiber 卸载时逆序执行 |
| Fiber 状态机(PENDING→LOADING→ACTIVE / FAILED / UNLOADING / DISPOSED) | `FiberState` Enum,同语义 |
| Loader(`cordis-plugin-loader`) | `loader.py`:读 manifest → 行(id/name/config/disabled)→ 拓扑激活 |
| Include patch(`cordis-plugin-include`) | `loader.py::apply_patches`:按行 id 定位,整行 config 替换,last-wins |
| `@mode` 事件文档 + 目录校验 | 事件词汇表 dataclass 声明派发模式,派发时运行时校验 |

## 4. 模块结构(遵守主规格目录规范)

```
codesage/kernel/
  base.py       # 契约层:Plugin/Service/Context 协议 + 类型
  events.py     # 事件词汇表(EventSpec dataclass + 注册表)+ 四派发器
  context.py    # Context 实现(服务仓库 + __getattr__ 代理 + effect 收集)
  fiber.py      # Fiber 生命周期状态机 + effect 逆序回滚
  registry.py   # 服务注册表 + inject 解析(按可用性激活)
  loader.py     # manifest 装载 + patch 应用(阶段 22 Profile/Bundle 的地基)
  __init__.py   # 显式导出公共 API
tests/kernel/test_{base,events,context,fiber,registry,loader}.py
```

## 5. API 草案(签名级,实现时细化)

```python
class Service:
    ctx_key: str                    # 挂到 ctx 的键,如 "llm"
    inject: list[str] = []          # 依赖的服务键
    Config: type | None = None      # 配置 schema(建议 dataclass)
    def __init__(self, ctx: Context, config): ...

class Context:
    def service(self, key: str) -> Any: ...       # 取服务(未就绪抛或等待)
    def provide(self, key: str, value: Any) -> None: ...
    def on(self, event: str, fn, *, prepend=False) -> Disposer: ...
    def emit(self, event: str, *args) -> None: ...          # 观察型,不等
    def waterfall(self, event: str, *args, next) -> Any: ... # 中间件链,可短路/包装
    def parallel(self, event: str, *args) -> None: ...       # 并发,等全部
    def serial(self, event: str, *args) -> Any: ...          # 顺序,前返回值传后
    def effect(self, execute, label="") -> Disposer: ...     # 注册副作用 + disposer
    def inject(self, keys, apply) -> Context: ...            # 子 ctx:等待依赖就绪后 apply

class Loader:
    def __init__(self, ctx: Context, manifest: list[Entry]): ...
    def mount(self) -> None: ...      # 按 inject 拓扑激活全部行
    def apply_patches(self, patches): ...  # id 定位,整行 config 替换,last-wins
```

## 6. 完成标准(验收清单)

1. **五概念单测**(`tests/kernel/` 全绿):
   - 服务注册/查找/键冲突;子 ctx 链式回退;`provide` 预置
   - inject:依赖缺失挂起、依赖就绪自动激活、循环依赖报错、加载顺序 = 拓扑序
   - 事件四派发语义:emit 注册序观察;waterfall `next()` 传递/短路/结果替换;parallel 并发全等;serial 前返回传后;prepend 优先级
   - effect:注册序逆序回滚、嵌套 fiber 卸载顺序、disposer 幂等
   - fiber 状态机:加载中/活跃/失败/卸载/销毁全转移 + 非法转移拒绝
2. **Loader + Patch**:manifest 行装载;disabled 行跳过;patch 按 id 替换 config last-wins;插入新行;loader 上下文注入(manifest 表达式求值,参照 DSH include `!!js` 的最小等价——本阶段仅支持字面量 + `$env:` 取值,表达式 DSL 留阶段 22)
3. **零新依赖**:pure stdlib(pydantic 已存在可用,不新增)
4. **内核自检 demo**(`python -m codesage.kernel.demo` 或单测内):Fake LLM 插件 + 工具插件 + 一个最小 loop 插件,manifest 装载跑通「输入 → 模型 → 工具 → 结果」
5. **兼容门**:既有 1400+ 测试全绿(内核纯新增模块,不允许破坏既有)
6. **理解文档** `docs/modules/21-plugin-kernel.md`:五概念 + 与 DSH/Cordis 的映射 + 设计决策记录

## 7. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 事件类型安全弱于 TS 声明合并 | 词汇表 dataclass + 派发时校验模式匹配(成本低、收益明确) |
| R2 | effect 回滚语义复杂(嵌套/并发) | fiber 单线程语义 + 逆序回滚单测覆盖;disposer 幂等 |
| R3 | 与既有 assemble.py 双轨运行 | 本阶段纯新增不接线;阶段 22 起 assemble 转 compat shim |
| R4 | 依赖注入过度设计 | 只做四件套(提供/查找/inject/effect),不做 HMR/热重载(留注释 ponytail: 天花板) |

## 8. 对照保留清单(主规格 20 条)

本阶段无模型/权限/工具语义,仅对齐 DSH 组合层;保留清单不动。

## 9. 实施步骤(依赖排序,每步可独立提交)

| 步 | 内容 | 闸门 |
|---|---|---|
| S1 | base.py + registry.py(服务仓库 + inject 拓扑) | tests/kernel/test_registry.py 绿 |
| S2 | context.py + fiber.py(生命周期 + effect) | tests/kernel/test_context.py + test_fiber.py 绿 |
| S3 | events.py(词汇表 + 四派发器) | tests/kernel/test_events.py 绿 |
| S4 | loader.py(manifest + patch) | tests/kernel/test_loader.py 绿 |
| S5 | demo(Fake LLM 最小 loop)+ 全量回归 | pytest tests/ -q 全绿;demo 可跑 |
| S6 | modules 理解文档 + 主规格/todo 同步 | 文档评审 + 合并 |
