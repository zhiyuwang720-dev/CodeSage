"""模型边界契约：控制平面与执行平面共同依赖的稳定标识。

控制平面只用它计算恢复指纹，执行平面用它声明当前边界实现版本。
放在 contracts 层是为了让两个平面都不必导入对方的实现（见架构边界用例）。
"""

from __future__ import annotations

# 模型边界实现版本。改变模型出口、配置快照语义、消息/工具转换或重试策略时必须递增：
# 恢复指纹随之变化，旧身份显式拒绝复用，而不是被悄悄改写。
MODEL_BOUNDARY_VERSION = "litellm_sdk_v1"

__all__ = ["MODEL_BOUNDARY_VERSION"]
