"""持久化服务定义(``ctx.sessionPersistence``)契约。

DSH index.ts 的 Python 移植。后端把 SessionEvent 作为事件溯源日志
存储,并单独携带不可重放的 SessionHeader 元数据。本文件只定义
契约面:抽象服务 + 词汇类型(SessionPersistenceSnapshot /
SessionInspection / SessionRawArtifact / SessionLocation)+ 两个
默认实现(readRaw 拒绝、prepare 走 load + SessionStore)。

与 Node 的差异(注释即文档):
- AbortSignal 参数在 Python 侧取消(见 coordinator 模块 docstring)。
- ``this.ctx.get('sessions')`` → ``self.ctx.sessions``(cordis-py 的
  Context 没有 get();未注册时根 ctx 的属性访问返回 False)。
- 抽象方法用 abc.abstractmethod;TS 的 ``readonly`` 字段由约定
  (docstring 声明)承载。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

from cordis import Service

from core.session import SessionPreparation

from .revision import SessionPersistenceRevision


class SessionPersistenceSnapshot:
    """轻量不可变源身份:不加载完整日志即可获得。

    一份已物化会话的分离元数据 + 一个不透明源限定令牌:日志每次
    变化令牌都变;不同后端各自的本地计数器即使数值相同也不可比
    (来源不同)。
    """

    __slots__ = ("header", "revision")

    def __init__(self, header: dict, revision: SessionPersistenceRevision) -> None:
        self.header = header
        self.revision = revision


class SessionInspection:
    """从持久化或活属主准备的不可变逻辑会话视图。

    调用方只能借用 header 与事件日志,不得修改 —— 返回值可能与
    活/已准备的不可变状态共享同一对象图。
    """

    __slots__ = ("meta", "events")

    def __init__(self, meta: dict, events: list) -> None:
        self.meta = meta
        self.events = events


class SessionRawArtifact:
    """一个会话的后端自有原始工件文本,逐字原样。

    ``content`` 是从后端物理编码解码后的原始文本,不是从解析后的
    事件重建 —— 因此保留后端特定的序列化(分块打包、键序、换行)。
    """

    __slots__ = ("meta", "filename", "content")

    def __init__(self, meta: dict, filename: str, content: str) -> None:
        self.meta = meta
        self.filename = filename
        self.content = content


class SessionLocation:
    """后端解析出的、按会话划分的本地工件位置。

    path 是绝对目标路径,可以指向尚未物化的工件。调用方只能把它
    当作位置提示,绝不能当作授权令牌。
    """

    __slots__ = ("kind", "path")

    def __init__(self, kind: str, path: str) -> None:
        self.kind = kind
        self.path = path


class SessionPersistence(Service, ABC):
    """耐久追加式会话存储。实现方保证连续、无损 JSON 可序列化的
    事件;``append`` 只在耐久后落定,``load`` 平衡完整的中断尾
    而不重写已提交事件。

    子类构造后即经 Service 基类注册为 ``ctx.sessionPersistence``。
    """

    provide = "sessionPersistence"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)

    @abstractmethod
    def locate(self, meta: dict) -> SessionLocation | None:
        """解析一个会话的后端独立工件位置,不读取/创建/冲刷。

        SQLite 这类不按会话持有一个工件的后端返回 None。
        """

    @property
    @abstractmethod
    def supportsRawArtifacts(self) -> bool:
        """后端是否按会话暴露逐字原始工件;声明 True 必须覆写 readRaw。"""

    async def readRaw(self, id: str) -> SessionRawArtifact | None:
        """读一个会话的后端工件文本逐字原样(默认实现拒绝)。

        调用方先测 supportsRawArtifacts;返回 None 只表示该会话
        没有已物化的工件。不按会话持工件的后端抛错。
        """
        raise RuntimeError("this session persistence backend does not expose raw artifacts")

    @abstractmethod
    async def create(self, meta: dict) -> None:
        """注册一个新会话的元数据。

        后端可以把物理写入推迟到第一次 append(惰性物化)——
        此时「创建过但从未 append」的会话不出现在 list 里:被
        遗弃的会话不留任何痕迹。
        """

    @abstractmethod
    async def append(self, id: str, events: list) -> None:
        """耐久持久化一批事件。

        遵守追加式与连续 seq 契约:首事件的 seq 必须等于存储的
        下一 seq(load 已耐久关闭任何中断回合之后)。拒绝含非 JSON
        可序列化 data 的事件,错误点出违规事件类型。
        """

    async def prepare(self, id: str) -> SessionPreparation:
        """准备 resume 用的精确未发布 Session(默认实现)。

        经 load 取不可变视图,再经 SessionStore.prepare 以持久化
        种子恢复。实现方可以在确认修订号仍当前后复用早前 inspect
        保留的对象图;释放收回未发布的预留。
        """
        loaded = await self.load(id)
        sessions = self.ctx.sessions
        if sessions is False:
            raise RuntimeError("cannot prepare a session: SessionStore is not configured")
        # structuredClone → JSON 往返:给恢复式构造一份新鲜分离图
        # (from_restore 要求独占所有权,不得别名持久化持有的对象)。
        return SessionPreparation.create(
            sessions.prepare(
                id,
                {
                    "seed": json.loads(json.dumps(loaded.events)),
                    "meta": json.loads(json.dumps(loaded.meta)),
                    "seed_source": "persistence",
                },
            )
        )

    @abstractmethod
    async def load(self, id: str) -> SessionInspection:
        """读不可变的平衡逻辑视图并提交所需的冷恢复。

        完整的中断末回合被保留并以缺失工具错误 + 打开的 step/turn
        边界耐久关闭;只有撕裂的末记录被丢弃。未知版本与已提交前缀
        中的损坏拒绝。绝不能崩溃修复仍绑定活会话的身份:平衡的活
        日志可以作为耐久快照返回,打开的活回合拒绝。
        """

    @abstractmethod
    async def inspect(self, id: str) -> SessionInspection:
        """检查不可变逻辑会话,不提交恢复也不发布。

        冷完整中断回合在内存里收到合成关闭器,撕裂物理尾保持
        原样;已活的会话返回其当前不可变快照(可能含打开回合与
        session/end-seed 边界)。协调器实现保留确切的冷未发布
        Session 供后续 prepare 有界复用。
        """

    @abstractmethod
    async def readFrom(self, id: str, fromSeq: int) -> dict:
        """从 fromSeq 起读存储事件 —— 读模型的续读原语。

        分离的物理后缀读:无准备缓存、无撕裂尾截断、无合成关闭器、
        不发布协调器状态。只返回有效连续存储前缀内的事件,撕裂
        碎片永不外泄;fromSeq 超出前缀返回空列表(非错误)。
        """

    @abstractmethod
    async def list(self) -> list:
        """元数据级轻量列举,不解析完整日志:每个已物化会话一个 header。"""

    @abstractmethod
    async def listSnapshots(self) -> list:
        """列出已物化会话与每个日志的廉价变化令牌。

        反复观察未变化的日志返回同一修订号;成功的变更式 load
        修复会改变下一次列出的修订号。
        """
