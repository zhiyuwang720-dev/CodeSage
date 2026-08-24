# session-persistence —— 持久化的接缝

core/session 的日志在内存里是完美的,但进程会退。谁把日志搬过
内存边界,谁决定「搬多少、何时搬、搬坏了怎么赔」?本包定义这条
接缝:一侧是**契约**(会话持久化服务长什么样),另一侧是
**协调器**(所有后端共用的编排大脑)。

## 契约与实现分层

- `index.py` 是纯契约:locate/readRaw/create/append/prepare/load/
  inspect/readFrom/list/listSnapshots,外加两个异常 —— 损坏
  (`SessionPersistenceCorruptionError`)与格式不支持
  (`SessionFormatUnsupportedError`,携带 location 指向原始工件)。
- `coordinator.py` 是编排中枢,只管**一致性**,不碰字节:
  - **每会话串行化**:同一 id 的全部写操作在一条 promise 链上排队,
    并发 append 不交错,`seq` 连续契约在写入端守得住。
  - **惰性物化**:create 不落盘,第一次 append 才出现 —— 空会话
    不占物理空间。
  - **write-behind 合并**:多次 append 按窗口合并成批落盘,失败
    时整批回退重试,顺序不乱。
  - **preparations**:加载/就绪/提交/预留四相位状态机 + LRU(容量
    5),预留期与提交期拒绝 append。
  - **创建采纳四情形**:已跟踪 / 磁盘前缀与活日志逐字节对齐则采纳 /
    cwd 不符或非前缀则拒绝(碰撞)/ 无工件则登记并持久化种子一次。
  - **退休排干**:detach 后异步刷盘落定,测试经
    `coordinator.retirements` 等待,不靠竞态猜测。

## invariant 住在哪

core/session 的 `Session.append` 只调 surface 校验(内部词法);
本包的存储边界才是 invariant 的消费点 —— `adoptStoredEvents` /
`snapshotStoredEvents` 内联校验存储记录的完整语义。invariant 是
配套插件,不是重复实现:活路径查一遍,存储路径查一遍,两遍校验
同一套规则。

## 测试

```bash
cd packages && python -m pytest session/session-persistence -q
```

协调器测试用真后端(JSONL)走全 8 个后端原语:惰性物化、修订号
轻量读与全量读一致、每会话串行、引用失败回滚、创建采纳四情形。
