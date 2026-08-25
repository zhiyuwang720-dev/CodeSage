"""耐久收件箱事件的增量投影(参考实现 agent/inbox.ts 实现)。

Inbox 是两条待办列表(next-turn / next-step)的活体投影:构造时
从会话日志重放一次 seed 边界后的全部 ``agent/inbox/spliced``
事件,之后每次活体变更(append/prepend/replace/remove/claim/
clear)都先写耐久 splice 事件、再改投影、再发布通知 —— 事件是
唯一事实源,投影是可丢弃的派生视图。

**投递序不变量**:耐久事件先于投影变更提交,所以同步的
``session/event`` 观察者读到的是 splice 前的列表,能从归一化
坐标重建被移除的消息。
"""

from __future__ import annotations

import math

__all__ = ["Inbox", "InboxNotifications"]


class InboxNotifications:
    """收件箱变更的活体通知回调(派发侧注入实现)。"""

    def inserted(self, message: dict) -> None: ...

    def discarded(self, message: dict) -> None: ...

    def claimed(self, message: dict, turn: int) -> None: ...


def _truncate_to_int(value, fallback: int) -> int:
    """JS 的 Math.trunc 语义:NaN/缺失落到 fallback。"""
    if value is None:
        return fallback
    try:
        truncated = math.trunc(value)
    except (TypeError, ValueError):
        return fallback
    if math.isnan(truncated):
        return fallback
    return truncated


class Inbox:
    """重放一次的投影,增量消费后来的收件箱 splice。"""

    def __init__(self, session, notifications: InboxNotifications) -> None:
        self._session = session
        self._notifications = notifications
        self._state: dict[str, list] = {"next-turn": [], "next-step": []}
        seed_length = session.header.get("seedLength") if session.header else 0
        for event in session.events[seed_length:]:
            if event.get("type") != "agent/inbox/spliced":
                continue
            try:
                self._apply(event["data"])
            except Exception as error:  # noqa: BLE001 -- 包装重放失败,保留原因为链
                raise ValueError(
                    f"invalid persisted inbox splice at session seq {event.get('seq')}"
                ) from error

    # ---- 只读面 ----

    @property
    def next_turn(self) -> list:
        """等待个体回合的提示。"""
        return self._state["next-turn"]

    @property
    def next_step(self) -> list:
        """等待下一个步骤边界的输入。"""
        return self._state["next-step"]

    @property
    def has_pending(self) -> bool:
        """任一待办列表是否含工作。"""
        return len(self.next_turn) > 0 or len(self.next_step) > 0

    # ---- 变更面 ----

    def clear(self) -> None:
        """耐久取消全部待办输入:先清 next-step,再清 next-turn。"""
        self.splice("next-step", 0, len(self.next_step), [])
        self.splice("next-turn", 0, len(self.next_turn), [])

    def claim(self, target: str, turn: int) -> list:
        """移除并返回为一个步骤提议的完整批次,发布每条已认领消息。

        耐久 splice 是纯删除。target 决定该边界是否同时消费一个
        排队的回合。返回 next-step 输入,随后是请求的排队回合。
        """
        claimed = self._mutate("next-step", 0, len(self.next_step), [], False)
        if target == "next-turn":
            claimed.extend(self._mutate("next-turn", 0, 1, [], False))
        for message in claimed:
            self._notifications.claimed(message, turn)
        return claimed

    def append(self, target: str, message: dict) -> None:
        """向一条待办列表追加消息并耐久记录插入。

        消息身份已在待办中时抛错。
        """
        self.splice(target, len(self._state[target]), 0, [message])

    def prepend(self, target: str, message: dict) -> None:
        """向一条待办列表头部插入消息并耐久记录插入。"""
        self.splice(target, 0, 0, [message])

    def replace(self, message_id: str, new_message: dict) -> bool:
        """原位替换一条待办消息,可能更换其身份。

        成功替换发布旧消息为 discarded、新消息为 inserted。
        @returns 消息是否仍在待办中。
        """
        location = self._locate(message_id)
        if location is None:
            return False
        self.splice(location["target"], location["index"], 1, [new_message])
        return True

    def remove(self, message_id: str) -> bool:
        """移除一条待办消息并耐久记录其取消。"""
        location = self._locate(message_id)
        if location is None:
            return False
        self.splice(location["target"], location["index"], 1, [])
        return True

    def splice(self, target: str, start: int, delete_count: int, inserted: list) -> list:
        """标准 splice 语义 + 耐久记录归一化结果。

        @returns splice 移除的消息。
        """
        return self._mutate(target, start, delete_count, inserted, True)

    # ---- 内部 ----

    def _locate(self, message_id: str) -> dict | None:
        """跨两条列表定位一条待办身份。"""
        for target in ("next-turn", "next-step"):
            for index, message in enumerate(self._state[target]):
                if message.get("id") == message_id:
                    return {"target": target, "index": index}
        return None

    def _mutate(self, target: str, start, delete_count, inserted: list, discard_removed: bool) -> list:
        """提交一次归一化变更并发布活体通知。

        归一化照 JS Array.prototype.splice:负 start 从尾计、
        越界钳制、NaN 落 0。
        """
        inbox = self._state[target]
        offset = _truncate_to_int(start, 0)
        actual_start = max(len(inbox) + offset, 0) if offset < 0 else min(offset, len(inbox))
        truncated_delete = _truncate_to_int(delete_count, 0)
        actual_delete_count = min(max(truncated_delete, 0), len(inbox) - actual_start)
        if actual_delete_count == 0 and len(inserted) == 0:
            return []
        outcome = "canceled" if (discard_removed and actual_delete_count > 0) else None
        splice: dict = {"target": target, "start": actual_start, "inserted": inserted}
        if actual_delete_count > 0:
            splice["removedCount"] = actual_delete_count
        if outcome is not None:
            splice["outcome"] = outcome
        self._validate(splice)
        event = self._session.append("agent/inbox/spliced", splice)
        removed = inbox[actual_start:actual_start + actual_delete_count]
        del inbox[actual_start:actual_start + actual_delete_count]
        inbox[actual_start:actual_start] = list(event["data"]["inserted"])
        if discard_removed:
            for message in removed:
                self._notifications.discarded(message)
        for message in event["data"]["inserted"]:
            self._notifications.inserted(message)
        return removed

    def _apply(self, splice: dict) -> list:
        """把一条归一化耐久 splice 施加到投影(重放路径)。"""
        self._validate(splice)
        inbox = self._state[splice["target"]]
        removed_count = splice.get("removedCount", 0)
        start = splice["start"]
        removed = inbox[start:start + removed_count]
        del inbox[start:start + removed_count]
        inbox[start:start] = list(splice["inserted"])
        return removed

    def _validate(self, splice: dict) -> None:
        """对照当前投影校验一条归一化 splice。"""
        inbox = self._state[splice["target"]]
        removed_count = splice.get("removedCount", 0)
        start = splice["start"]
        if (
            not isinstance(start, int)
            or start < 0
            or start > len(inbox)
            or not isinstance(removed_count, int)
            or removed_count < 0
            or start + removed_count > len(inbox)
        ):
            raise ValueError("invalid inbox splice")
        candidate = inbox[:start] + list(splice["inserted"]) + inbox[start + removed_count:]
        ids = set()
        other = self._state["next-step"] if splice["target"] == "next-turn" else self._state["next-turn"]
        for message in (other + candidate) if splice["target"] == "next-turn" else (candidate + other):
            if message.get("id") in ids:
                raise ValueError(f'message "{message.get("id")}" is already pending')
            ids.add(message.get("id"))
