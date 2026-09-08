"""运行时引擎层(06-P5): 纯 review 单语义 ReAct 编排, 独立 services/runtime/。

布局:
- config.py      RuntimeStack(legacy/runtime 栈枚举, 兼容旧会话识别)
- bridge.py      RuntimeBridge(审查入口: 建 session/跑循环/收尾 payload)
- adapters/      RuntimeSessionAdapter(session 生命周期编排)
- runner.py      多轮 runner(收敛 session.state)
- query_loop.py  单轮主循环(工具编排/降级/压缩/终点判定)
- query_*.py     附属于单轮循环的上下文/消息/令牌/停钩子等纯逻辑
- compaction/    长会话压缩(自动/截断/重建)
- transcript.py  DB 消息 ↔ TranscriptItem 映射
"""
