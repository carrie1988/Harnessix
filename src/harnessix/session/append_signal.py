"""Session追加通知：提交成功后在进程内把追加后的Thread告知订阅者。"""

from __future__ import annotations

from collections.abc import Callable

from harnessix.agent.models import Thread


class AppendSignal:
    """追加提交后的进程内提示，只表示“现在重读能看到这次追加”。

    通知不是事实来源：订阅者必须仍从存储读取，并保留自己的定时重读。漏发（提交后进程内故障、
    不经本存储追加入口的同事务写入）只让订阅者晚一个重读周期，多发只多一次读取。
    """

    def __init__(self) -> None:
        self._listeners: set[Callable[[Thread], None]] = set()

    def subscribe(self, listener: Callable[[Thread], None]) -> Callable[[], None]:
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def notify(self, thread: Thread) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(thread)
            except Exception:
                # 追加已经提交；订阅者的故障不能让调用方把它当作写入失败。
                continue
