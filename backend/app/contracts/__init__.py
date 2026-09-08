"""契约层(06-P2): 引擎最低共享依赖, 不含运行时编排。

依赖单向序: contracts ← permission ← session ← {memory, skill} ← tooling ← hooks ← runtime
models.py / query_state.py / final_review_contract.py 承载引擎共享契约;
tools.py 为 RuntimeTool ABC + ToolExecutionContext(引擎 tool 契约基础)。
"""
