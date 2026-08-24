"""本包的不变式 companion 占位(DSH 的 session-persistence/invariant)。

DSH 中本文件向 ``@deepseek-ai/dsh-invariants`` 服务注册一个
**无运行时逻辑** 的 companion:持久化正确性由后端往返测试与崩溃尾
测试承载,本包没有进程内可观察的关系需要持续断言 —— 所以
install 是空操作。真正的校验在协调器读路径内联执行
(见 coordinator 的 snapshotStoredEvents/adoptStoredEvents)。

Python 移植:CodeSage 没有 dsh-invariants 服务,cordis-py 也没有
``ctx.invariants`` 内建 —— 本模块保留 DSH 的包名/注册形状作为
契约面,``apply`` 在原环境会注册到 invariants 服务,这里仅是
无副作用占位(注释即文档,避免未来引入 invariants 时漏掉本包)。
"""

from __future__ import annotations

#: Cordis companion 插件名(照 DSH 逐字)。
name = "session-persistence-invariant"

#: companion 注册前需要先安装的服务(照 DSH 逐字;Python 侧无此服务,
#: 保留声明以反映依赖形状)。
inject = ["invariants"]


def install() -> None:
    """无运行时不变式:正确性由后端往返与崩溃尾测试承载。"""


def apply(ctx) -> None:
    """注册本包的 companion(照 DSH 的调用面)。

    Python 移植为占位:无 invariants 服务可注册,调用方也无人消费
    返回值;保留签名以便未来接入时行为不变。
    """
