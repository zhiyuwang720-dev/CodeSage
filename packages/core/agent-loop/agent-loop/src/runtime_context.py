"""动态运行时上下文的耐久投影状态。

运行时上下文(system-prompt 装配的动态分节拼接)是「模型每步
都该知道的世界状态快照」。它不能只存在于内存 —— 重放、恢复、
UI 桥都要能从会话日志重建它;但也不能每步都写 —— 上下文没变
时再写一条就是日志噪音。所以投影只产出「值得写」的候选:当前
文本与上一次保留的快照不同时才产出消息(首次且上下文为空则不
写;上下文清空写 CLEARED 声明)。已产出的消息照常入日志 ——
投影不提交它,提交权在调用方(agent 循环把投影结果折进 pre-step
消息批次)。

**self-healing retained**:构造时反向扫描日志恢复上一次快照,之后
跟随 ``session/event`` 权威事件流:新的插件快照消息更新保留值,
快照消息被表面替换(compaction)时清除保留值。投影状态永远是
日志的派生视图 —— 类比数据库的物化视图:底层日志(write-ahead
log)是唯一事实源,视图可随时丢弃重建,永不与日志脱同步。
"""

from __future__ import annotations

from llm.llm.src.messages import create_user_message

from core.session.src.surface import is_replacement_surface_event

__all__ = ["CLEARED", "RuntimeContextProjection", "SOURCE"]

#: 插件来源标识:只有 system-prompt 插件写的快照由本投影跟踪。
SOURCE = "@deepseek-ai/dsh-system-prompt"

#: 上下文清空时的占位文本:声明此前的快照不再适用。
CLEARED = "Current runtime context: none. Earlier runtime-context snapshots no longer apply."

#: ``retained`` 的未定义语义:尚无快照存在过(JS 的 undefined)。
_MISSING = object()


def _is_owned(message: dict) -> bool:
    """消息是否由本插件系统提示写入(插件来源 + 本插件标识)。"""
    source = message.get("source") or {}
    return source.get("kind") == "plugin" and source.get("plugin") == SOURCE


def _text_of(message: dict) -> str | None:
    """快照消息的规范文本:单一 text 块的内容,否则视为无文本。"""
    content = message.get("content") or []
    if len(content) == 1 and content[0].get("type") == "text":
        return content[0].get("text")
    return None


class RuntimeContextProjection:
    """跟踪最后一条保留的运行时上下文快照,不拥有它的提交。

    ``retained`` 三态:``_MISSING`` = 从未有过快照;``None`` = 不再
    保留(清空或替换之后);``dict`` = 当前保留的 {seq, text}。
    """

    def __init__(self, ctx, session) -> None:
        self._ctx = ctx
        self._session = session
        self._retained = _MISSING
        surface = set(session.surface.nodes)
        # 反向扫描:最近的在表面上的插件快照是保留值;只见过不在
        # 表面上的快照则确认「无保留」—— 被 compaction 遮蔽过的
        # 快照已从派生历史上消失,不算保留值。
        for event in reversed(session.events):
            if event.get("type") != "user/message" or not _is_owned(event["data"]):
                continue
            if self._retained is _MISSING:
                self._retained = None
            if event["seq"] in surface:
                self._retained = {"seq": event["seq"], "text": _text_of(event["data"])}
                break
        # 跟随权威事件流:新快照更新保留值;快照被表面替换(compaction
        # 遮蔽)时清除 —— 投影不自己提交,所以监听器只读日志事实。
        ctx.on("session/event", self._on_session_event)

    def _on_session_event(self, subject, event) -> None:
        if subject is not self._session:
            return
        if event.get("type") == "user/message" and _is_owned(event["data"]):
            self._retained = {"seq": event["seq"], "text": _text_of(event["data"])}
        elif self._retained is not _MISSING and self._retained is not None \
                and is_replacement_surface_event(event) \
                and self._retained["seq"] in (event.get("sourceEventSeqs") or []):
            self._retained = None

    def project(self, current: str, sections: list) -> dict | None:
        """只在保留值与当前渲染不同时产出候选快照消息。

        这是去重决策:类比操作系统的日志压缩 —— 相同内容不重复
        落盘,只有变化才写。没有变化时返回 None,调用方不折进
        消息批次,模型看到的历史就是上一条快照。

        @param current - 完整渲染的动态上下文文本;空串 = 上下文为空。
        @param sections - 组成当前快照的命名贡献(system-prompt 的
            已渲染分节);为空时快照不携带 form/sections 归属。
        @returns 候选 user 消息(调用方折进 pre-step 批次),或 None。
        """
        if self._retained is _MISSING and len(current) == 0:
            return None
        snapshot = CLEARED if len(current) == 0 else current
        if self._retained is not None and self._retained is not _MISSING \
                and self._retained["text"] == snapshot:
            return None
        source = {"kind": "plugin", "plugin": SOURCE}
        if len(sections) > 0:
            # form 是语义分类(snapshot),sections 保留生成它的贡献名,
            # 供 UI 桥在重放时复刻同一张卡。
            source["form"] = "snapshot"
            source["sections"] = list(sections)
        return create_user_message({
            "content": [{"type": "text", "text": snapshot}],
            "source": source,
        })
