"""取消信号原语。

取消是 harness 里最横切的关注点:agent 循环、工具派发、维护
任务共享同一条取消链。设计上每条取消链由一个 signal 沉淀原因,
消费点在检查点 ``throw_if_aborted()`` 抛规范化异常 —— 这借鉴
操作系统的信号机制:handler 注册在信号上,进程在安全点检查
信号而不是随时被打断。Python 没有内建等价物,包内自建最小实现。

语义要点:

- **原因沉淀**:``abort(cause)`` 只做一件事 —— 把原因写到
  controller 上并唤醒监听者;取消的传播是惰性的,消费点在自己的
  检查点读取原因,所以「谁取消」与「何时生效」解耦;
- **幂等**:第一次 abort 生效,重复 abort 被忽略 —— 取消是状态
  转移不是事件,后到者不覆盖先到者的原因(与操作系统的信号量
  一次只消费一次同构);
- **融合**:调用方、属主 fiber、工厂拆解都可能发起取消,多个源
  合并成一个 signal,任一源中止即中止。
"""

from __future__ import annotations

__all__ = ["AbortController", "AbortError", "AbortSignal", "any_signals"]


class AbortError(Exception):
    """``signal.throw_if_aborted()`` 抛出的规范化异常。

    reason 可以是任意值(不限于异常对象);Python 需要异常才能
    ``raise``,这里把任意 reason 包一层,原 reason 保留在
    ``.reason`` —— 消费方分类取消原因时不丢信息。
    """

    def __init__(self, reason) -> None:
        super().__init__(str(reason))
        self.reason = reason


class AbortSignal:
    """最小取消信号:aborted/reason + 同步监听者列表。

    监听者语义:注册即读当前状态(已中止的 signal 立即回调一次,
    避免竞态 —— 注册前取消的事故不会静默丢失);abort 幂等(首次
    调用之后重复 abort 被忽略)。
    """

    def __init__(self) -> None:
        self._aborted = False
        self._reason = None
        self._listeners: list = []

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def reason(self):
        return self._reason

    def add_listener(self, callback) -> None:
        """注册同步回调;已中止时立即以当前 reason 回调一次。"""
        if self._aborted:
            callback(self._reason)
            return
        self._listeners.append(callback)

    def remove_listener(self, callback) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def throw_if_aborted(self) -> None:
        """中止检查点:已中止则抛规范化异常,否则无事发生。"""
        if self._aborted:
            reason = self._reason
            if isinstance(reason, BaseException):
                raise reason
            raise AbortError(reason)

    def _abort(self, reason) -> None:
        """内部中止提交:首次调用生效,通知全部监听者。"""
        if self._aborted:
            return
        self._aborted = True
        self._reason = reason
        for listener in list(self._listeners):
            listener(reason)
        self._listeners.clear()


class AbortController:
    """中止控制器:``abort(reason)`` 触发 signal 的单一中止。"""

    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason=None) -> None:
        self.signal._abort(reason)  # noqa: SLF001 -- 同模块内部原语


def any_signals(signals: list) -> AbortSignal:
    """融合多个取消源:任一源中止即中止融合信号。

    类比操作系统的 fd 集合监听:多个事件源轮询合并成一次等待,
    任一源就绪即唤醒。任一源已中止时立即中止(以第一个已中止源
    的原因为准 —— 先到先得,与 abort 幂等一致)。
    """
    fused = AbortSignal()
    for source in signals:
        if source.aborted:
            fused._abort(source.reason)  # noqa: SLF001 -- 同模块内部原语
            return fused
    for source in signals:
        source.add_listener(fused._abort)  # noqa: SLF001 -- 同模块内部原语
    return fused
