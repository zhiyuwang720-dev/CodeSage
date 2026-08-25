"""一条 agent 日志如何为它消费的工作记账(参考实现 agent/consumed-work.ts 实现)。

回合与步骤词表单独回答不了这个问题:一个在首步骤前停下的回合,
其 ``turn/end`` 形状与「拒绝或空认领产生的平衡空操作回合」完全
相同 —— 只读回合要么把砍掉的工作记成已完成,要么给每个空操作
定罪。缺失的事实是收件箱自己的记录:Inbox 的每次变更都带
``removedCount`` 入账,取消则标 ``outcome: 'canceled'`` —— 这
把「回合认领其输入」与「工作未被运行就被丢弃」分开。
"""

from __future__ import annotations

__all__ = ["ConsumedWork", "accounts_for_claim", "fold_consumed_work"]


class ConsumedWork:
    """一条 agent 日志对消费工作的记账结果。

    - end:最近一个为消费工作记账的已关闭回合 —— 进入过模型步骤,
      或认领过收件箱输入后失败/停止/被拒。没有任何回合关闭在任何
      工作之上时为 None。
    - dropped_unrun:该回合之后,已接受的工作是否未经运行就从
      收件箱被取消。这是取消在任一回合打开之前拿走输入的唯一
      账目 —— 没有 turn/end 描述它。
    """

    def __init__(self, end: dict | None = None, dropped_unrun: bool = False) -> None:
        self.end = end
        self.dropped_unrun = dropped_unrun


def accounts_for_claim(reason: dict) -> bool:
    """一个消费了输入却从未到达步骤的回合,其结尾是否为输入记账。

    只有 ``completed`` 不记账:一旦认领被改写掉,它就没什么可跑
    了。``blocked`` 也是那个输入的终结 —— 产生它的步骤前拒绝
    丢弃了已认领的消息,拿走的活永远不会跑。
    """
    kind = reason.get("kind")
    if kind == "completed":
        return False
    if kind in ("blocked", "aborted", "interrupted", "error"):
        return True
    # 未知结尾:默认归账。唯一边缘是 max-tokens —— 它必须有步骤,
    # 其回合在到达这里前就已短路为 stepped;而词表可扩展,后端
    # 追加的变体无法穷举 —— 消费了输入却叫不出名字的结尾,
    # 不得读作成功。
    return True


def fold_consumed_work(events: list) -> ConsumedWork:
    """把一条 agent 日志(或其属主后缀)折叠成消费工作账目。

    单趟,且每个输入都是日志本身:没有调用方需要在取消前采样活
    状态 —— 由任何人(属主拆解、祖先中断、卸载中的插件)发出的
    取消都读到同一份账目。
    """
    stepped: set[int] = set()
    claimed: set[int] = set()
    open_turn: int | None = None
    end: dict | None = None
    dropped_unrun = False
    for event in events:
        data = event.get("data", {})
        if event.get("type") == "turn/start":
            open_turn = data.get("turn")
        elif event.get("type") == "step/start":
            stepped.add(data.get("turn"))
        elif event.get("type") == "agent/inbox/spliced":
            removed_count = data.get("removedCount")
            if removed_count is None:
                continue
            # 替换让工作以新身份继续待办,只有什么都不留的取消
            # 才丢弃它。
            if data.get("outcome") == "canceled":
                dropped_unrun = dropped_unrun or len(data.get("inserted", [])) == 0
            # 认领是循环自己的步骤边界读,总在回合内。
            elif open_turn is not None:
                claimed.add(open_turn)
        elif event.get("type") == "turn/end":
            turn = data.get("turn")
            open_turn = None
            if (turn in stepped) or (turn in claimed and accounts_for_claim(data.get("reason", {}))):
                stepped.discard(turn)
                claimed.discard(turn)
                end = event
                # 该回合关闭前丢弃的任何东西由它自己的结尾报告;
                # 只有之后的丢弃仍未记账。
                dropped_unrun = False
    return ConsumedWork(end=end, dropped_unrun=dropped_unrun)
