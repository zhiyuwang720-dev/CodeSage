# core/session —— 会话的事件溯源内核

一个会话的本质是什么?直觉上是「一段对话的最新状态」;但在一个
追求可恢复性的 harness 里,会话的**历史**才是真相,状态只是从
历史推导出来的投影。本包把这条原则做成了硬约束:

- **append-only 事件日志是唯一事实源**。任何事件一旦入日志就冻结,
  不可改写、不可删除。`seq = log.length` —— 序号就是位置,连续性
  不是约定而是构造。
- **消息历史是纯函数投影**。从日志折叠出「当前表面」(surface),
  表面不存状态,只在对数上重放。日志怎么走,表面就怎么演化;
  日志不会撒谎,表面就不会。

## 包的形状

事件词汇(`known_event_types.py` 的 13 种事件)、消息/配置类型
(`types.py` 手写校验器替代 Zod)、无损 JSON(`json.py`,自写
迭代式快照器拒绝负零/稀疏/循环/非平凡原型)、不变量
(`invariant.py` 校验 seq 连续、回合/步骤嵌套、调用配对)、表面
折叠(`surface.py` 三类消息折叠与溯源断言)、存储词汇打包
(`chunk_rows.py`:碎块事件打包成行,MIN_RUN=3)、崩溃关闭器
(`repair.py`:中断回合合成关闭器)、请求头折叠
(`request_header.py`)、种子边界事件、`Session`(append 同步校验
入日志后通知观察者)与 `SessionStore`(生命周期:prepare → enter
→ announce → flush)。

## 两层词汇,一个日志

`Session.append` 只做一件事:校验候选事件对当前日志的合法性
(seq 连续、嵌套正确、surface 可接纳),通过则入日志。校验失败的
候选绝不污染表面 —— 计划先行,失败无痕。这是整个系统的信任根:
日志里只有合法事件,下游的投影、持久化都可以放心重放。

## 生命周期:prepare → enter → announce → flush

`SessionStore` 把「构造对象」与「成为活会话」拆成四步,使抛错的
监听者能否决创建并配对回滚:

1. **prepare**:校验 id/cwd,构造 Session(种子事件经与活 append
   相同的通道接纳)。带种子时在日志末尾追加 `session/end-seed`
   边界事件 —— 后端捕获创建种子时标记已在,加载路径无写入。
2. **enter**:安装发布钩子、入仓库,返回一次性 detach。
3. **announce**:发出恰好一次 `session/created`;同步抛错的监听者
   否决发布,随之 yield 的 detach 触发配对销毁边。
4. **flush**:派发被等待的 `session/flush` 耐久检查点。

与 DSH 的刻意差异:typert 是包内自建轻量注册表(DSH 为全局注入,
后续需要再提为独立包);`seed_source="persistence"` 的恢复路径
直接 `from_restore` 构造,与 `seed=[]` 的新建路径共用同一构造器。

## 测试

```bash
cd packages && python -m pytest core/session -q
```

含三包全链路 e2e:建会话 → 追加回合 → flush 落盘 → 新世界重载
→ 事件逐条一致 → resume 续写(种子末尾重标 end-seed,DSH 语义)
→ 再落盘验证;以及跨世界撕裂尾修复。
