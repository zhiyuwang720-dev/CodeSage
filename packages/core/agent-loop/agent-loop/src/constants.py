"""agent-loop 部署常量。

每 agent 步骤并发在飞的工具调用上限 —— 类比操作系统的线程池
大小:并发不是免费的,无界并行会让工具风暴瞬间打满外部资源,
有界并行把最坏情况锁在可预期之内。``1`` 即串行;省略时回落到
本常量。它同时是 tools 插件 code 模式子派发上限的默认值
(core/tools 的 maxParallelSubCalls 默认 10,两侧同源 —— 顶层
并发与子派发并发共享同一个护栏数值)。
"""

DEFAULT_MAX_PARALLEL_TOOL_CALLS = 10
