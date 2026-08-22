# 阶段 21:插件内核(Python 版 Cordis)(理解文档)

> 权威设计:`docs/specs/21-plugin-kernel.md`(实现时逐字执行)。本文是设计摘要 + 决策记录 + 实现期关键裁决(S1-S6 交付,全部测试绿,2026-08-22)。

## 设计摘要

CodeSage 到阶段 20 为止的装配是硬编码组合根(assemble.py)。V2 插件化转向的第一步 = 交付**纯 stdlib 的 Python 版 Cordis 内核**:Cordis 本体(TS 2709 行)不能从 Python 复用,不引 cordis-py(第三方移植 = 新依赖 + API 绑定),照译 `vendor/cordis/src` 九文件实现五概念 + Loader + Patch,为阶段 22-25 全面改造提供组合层。

- **插件即服务**:`Service` 基类(`ctx_key`/`inject`/`Config`)或普通函数 `apply(ctx, config)`;生命周期挂载到上下文
- **上下文即服务仓库**:`ctx.<key>` 稳定键注册/查找,键查找替代 import;子 ctx 链式回退
- **inject 声明依赖**:所需服务未就绪则挂起(PENDING),就绪后自动激活;加载顺序 = 依赖推导的拓扑序
- **类型化事件**:`emit`(同步观察)/ `waterfall`(中间件链,可 veto)/ `parallel`(并发,等全部)/ `serial`(顺序,首 bail 即返)/ `bail`(同步短路)五派发
- **可逆副作用**:`ctx.effect(execute)` 收集 disposer,插件卸载逆序回滚(TS 同款)
- **Loader + Patch**(阶段 22 Profile/Bundle 的地基):manifest 行装载(id/name/config/disabled)、按 id patch、config last-wins

### 文件映射(vendor/cordis/src 九文件 → kernel)

| vendor TS | Python | 职责 |
|---|---|---|
| `context.ts` | `context.py` | Context:服务仓库 + `__getattr__` 代理 + 子 ctx 链式回退 + effect 收集 |
| `events.ts` | `events.py` | EventsService:Hook 注册表 + 五派发 + `internal/listener`/`internal/update` 特例 |
| `fiber.ts` | `fiber.py` | Fiber 状态机(PENDING→LOADING→ACTIVE/FAILED/UNLOADING/DISPOSED)+ 逆序回滚 |
| `internal.ts` | 并入 `events.py` + `fiber.py` | `internal/update` 桥接、`_hooks` 表(Python 无单独 internal 模块) |
| `logger.ts` | `logger.py` | printf 风格日志(`%s/%o/%c` formatter,TS 逐字节语义) |
| `reflect.ts` | `reflect.py` | ReflectService:Impl 声明 + 缺服务反射查找 + `_FilterCtx` |
| `registry.ts` | `registry.py` | RegistryService + Runtime(同插件多 fiber 共享)+ `resolve_inject` |
| `service.ts` | `service.py` | Service 基类 + `FILTER` 隔离标签 |
| `utils.ts` | `utils.py` | `is_special_property`/`is_object`/`AggregateError`/`DisposableList` |

另:`loader.py`(cordis-plugin-loader 语义子集)+ `demo.py`(Fake LLM 自检)。

## 设计决策记录(spec 核心裁决)

1. **自研 Python 版内核,零新依赖(裁决 1/§1)** — 不引 cordis-py;pydantic 已存在可用,其余全 stdlib。
2. **照译九文件而非重构(裁决 2,用户强制)** — 「实现一模一样的 Cordis,只是 Python 版本」:结构/命名/语义逐文件对齐,不引入 TS 没有的东西。
3. **Loader 最小等价(裁决 3/§6.2)** — 只做 manifest 行装载 + 按 id patch + last-wins + `$env:` 插值;cordis-plugin-loader 的 import 机制、EntryGroup/EntryTree 持久化、isolate/intercept、HMR 留阶段 22(表达式 DSL 也留 22)。
4. **事件类型安全不另做词汇表(裁决 4/§4 偏离)** — 规格草案提过 EventSpec dataclass;对齐重写后保持 TS 原样(Hook + 派发时 `is_bailed` 检查),R1 风险按原样接受。
5. **行级 inject 用 apply 对象形状(裁决 5/§6)** — manifest 行声明 inject 时包装为 `{"inject": [...], "apply": fn}` 交给 kernel registry 既有机制,拓扑激活完全复用,Loader 不重写依赖逻辑。
6. **dispose/卸载是 fire-and-forget(裁决 6,TS 语义)** — `registry.delete()` 的 fiber.dispose 是异步微任务;Python 用 `asyncio.ensure_future` 等价调度。

## 实现期关键裁决(照译期间的坑,后续阶段直接复用)

1. **`_spawn` 必须 guard `get_running_loop()`** — Python 3.12 里 loop 外的 `ensure_future` 不报错,而是建孤儿任务挂到隐式 loop;之后在新 `asyncio.run` 里 `await` 会抛 "attached to a different loop"。无 loop 时直接返回裸协程。
2. **`asyncio.gather()` 只能在 loop 内构造** — loop 外调用返回 `_GatheringFuture` 而非协程,`asyncio.run` 拒绝。测试统一用「loop 内 gather」helper。
3. **hasattr 陷阱** — 根 Context 的 `__getattr__` 缺服务时返回 None(永不抛 AttributeError),`hasattr(ctx, anything)` 恒 True。成员判断用 `"x" in ctx`(`__contains__`)。
4. **`CordisError.INACTIVE_EFFECT` 常量就是完整消息串** — 直接用作 `code`,测试按 `e.value.code` 比对。
5. **形参命名遮蔽 builtin** — `events.py` 的 `update_bridge` 参数原名 `next` 遮蔽 `next(it)`,引发 "next_() takes 0 positional arguments";改名 `next_fn`。
6. **registry 插件三种形状** — 函数 / class / **dict**(`{"inject", "apply", "name"}`);TS 属性访问在 dict 上不存在,统一走 `_plugin_attr` helper(`_is_applicable`/`resolve`/`plugin` 全用)。
7. **`fiber.update()` 对非 ACTIVE(PENDING)返回 None** — loader 的 `apply_patches` 必须 `if result is not None: await result`,否则 `await None` 炸。
8. **patch last-wins = 每次 update 一次完整重启** — 同 id 两个 patch 触发两次 fiber restart(`[v1, v2, v3]` 全被观察到),最终 config 生效;测试按此固化(TS entry.update 同款)。
9. **监听者归属 root fiber** — `ctx.on` 经 mixin 绑定注册到 root,插件卸载**不**移除监听者;只有显式 disposer 才移除(测试改名固化)。
10. **logger format 是 TS regex 语义** — `%([a-zA-Z%])`:未知 format 如 `%.1f` 原样保留**且**未消费参数追加;`%o` = JSON.stringify 无空格(`json.dumps(separators=(",", ":"))`);Error 展开发生在 format 时;尾部 `typeof arg === 'object'` 追加检查 → `isinstance(arg, (dict, list, tuple))`。
11. **parallel 错误收集 = TS safeCollect 等价物** — `asyncio.gather(return_exceptions=True)` 收集后抛 `AggregateError`。
12. **`_dispatch_this` 桥接 TS `bind`** — TS 监听者经 `callback.bind(thisArg)` 获得 `this`;Python 无绑定,派发时记录当前 fiber,`internal/update` 桥接从这里读 `_hooks`(退化为 root 保底)。
13. **Python 异常无恒等** — `ValueError("a") == ValueError("a")` 为 False;断言用 `isinstance`/`str()`。

## 红线固化

| 红线 | 锚点 | 状态 |
|---|---|---|
| 零新依赖 | 全部 import = stdlib(+既有 pydantic);无新增 pyproject 依赖 | ✓ |
| 纯新增不接线 | 无模块 import `kernel`;assemble.py/engine 未动 | ✓ |
| 既有测试全绿 | 全量回归 **1508 passed, 9 skipped** | ✓ |
| 对照保留清单不动 | 本阶段无模型/权限/工具语义 | ✓ |
| Kode-CLI / backend 不碰 | 只读参考(照译 source 为 vendor/cordis/src) | ✓ |

## 交付与验证

- **S1**:registry + test_registry — 绿(插件三形状/runtime 共享/inject/delete fire-and-forget/counter)
- **S2**:context + fiber — 绿(`__getattr__` 代理/子 ctx 链式回退/effect 逆序/状态机)
- **S3**:events — 绿(五派发/waterfall veto/prepend/监听者归属)
- **S4**:loader — 绿(行装载/disabled 跳过/拓扑激活/循环 PENDING/补丁 last-wins/插入/禁用/$env: 插值)
- **S5**:demo + 全量回归 — `python -m codesage.kernel.demo` 跑通「输入 → 模型 → 工具 → 结果」(Fake LLM 两轮应答 + calc 工具 + loop 插件);**1508 passed, 9 skipped**(基线 1432 + 内核 76 新增)
- **S6**:本文档 + 主规格同步 + todo 勾选 + 合并 master + push

## 与路线图的关系

- **依赖**:无(纯新增模块,stdlib 独立);参考:DSH `docs/cordis-primer.md` + `vendor/cordis/src` + cordis-mini(只读)
- **被依赖**:
  - 22 boot — Loader/Patch 扩展(表达式 DSL `!!js`、EntryGroup/EntryTree、import 机制)+ Profile/Bundle + assemble 转 compat shim
  - 23 seams — llm/tools/permissions/hooks 四服务进 ctx,manifest 单行换实现
  - 24 session-scope — 每 agent 隔离 ctx(注册随 agent 销毁回滚)
  - 25 finalize — 全模块迁移收尾,assemble shim 下线
