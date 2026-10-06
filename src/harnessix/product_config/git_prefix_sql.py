"""专用Git前缀连接的SQL合作中断窗口；由宿主明确持有，不覆盖共享连接。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import local
from weakref import WeakValueDictionary

from harnessix.agent.errors import KernelError

_owned = local()


def _transaction_token(statement: str) -> str:
    """跳过前导SQL注释，仅复制短关键字，不保存或分割可能包含私有正文的尾部。"""
    offset = 0
    while offset < len(statement):
        if statement[offset].isspace() or statement[offset] in {";", "\ufeff"}:
            offset += 1
            continue
        if statement.startswith("--", offset):
            end = statement.find("\n", offset + 2)
            offset = len(statement) if end < 0 else end + 1
            continue
        if statement.startswith("/*", offset):
            end = statement.find("*/", offset + 2)
            if end < 0:
                return ""
            offset = end + 2
            continue
        break
    end = offset
    while end < len(statement) and statement[end].isalpha():
        end += 1
    return statement[offset:end].upper()


class _SQLCheckpoint:
    """驱动只返回中断信号；保存并在上层传播首次原取消/期限异常。"""

    def __init__(self, checkpoint: Callable[[], None]) -> None:
        self.checkpoint = checkpoint
        self.interrupted: BaseException | None = None
        self.epoch = 0
        self.windows: WeakValueDictionary[int, object] = WeakValueDictionary()

    def trace(self, statement: str) -> None:
        """仅识别事务边界，不保存SQL或正文；任何回滚/提交都撤销旧窗口。"""
        token = _transaction_token(statement)
        if token in {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}:
            self.epoch += 1

    def interrupt(self) -> int:
        try:
            self.checkpoint()
        except BaseException as error:
            if self.interrupted is None:
                self.interrupted = error
            return 1
        return 0


def require_git_prefix_sql_window(database: sqlite3.Connection) -> None:
    """有限端口不接受没有SQL取消窗口的原始连接。"""
    control = getattr(_owned, "connections", {}).get(database)
    if control is None:
        raise KernelError("git_prefix_sql_control_required", "Git前缀需要专用SQL检查点窗口")
    if control.interrupted is not None:
        raise control.interrupted
    # 不能只依赖1000步回调；小表和await之后仍须立即消费原Owner/Scope/绝对期限。
    control.checkpoint()


def git_prefix_transaction_epoch(database: sqlite3.Connection) -> tuple[object, int]:
    """窗口只在同一SQL控制实例与事务边界代际内有效，不持久化或充当Fence。"""
    require_git_prefix_sql_window(database)
    control = _owned.connections[database]
    return control, control.epoch


def _register_git_prefix_write_window(database: sqlite3.Connection, window: object) -> None:
    """仅begin在完成认证后登记原实例；自造weakref不能替代该控制窗口内来源。"""
    require_git_prefix_sql_window(database)
    _owned.connections[database].windows[id(window)] = window


def _require_issued_git_prefix_window(database: sqlite3.Connection, window: object) -> None:
    """验证仍由原SQL控制窗口登记的实际实例，不读取Key或执行授权。"""
    require_git_prefix_sql_window(database)
    if _owned.connections[database].windows.get(id(window)) is not window:
        raise KernelError("publication_history_unproven", "Git发布窗口来源无法核验")


@contextmanager
def git_prefix_sql_window(
    database: sqlite3.Connection, *, checkpoint: Callable[[], None]
) -> Iterator[None]:
    """显式独占专用连接的进度回调，不控制事务；禁止在已有共享回调上嵌套。

    宿主须为该窗口提供未装配其他进度回调的专用连接，并将原取消、Owner、
    Key与绝对期限检查合成同一检查点。该窗口不创建新期限，不获取业务锁。
    """
    checkpoint()
    connections = getattr(_owned, "connections", None)
    if connections is None:
        connections = {}
        _owned.connections = connections
    if database in connections:
        raise KernelError("git_prefix_sql_control_conflict", "Git前缀连接已有SQL检查点窗口")
    control = _SQLCheckpoint(checkpoint)
    database.set_progress_handler(control.interrupt, 1000)
    database.set_trace_callback(control.trace)
    connections[database] = control
    try:
        yield
    except BaseException:
        if control.interrupted is not None:
            raise control.interrupted from None
        raise
    else:
        if control.interrupted is not None:
            raise control.interrupted from None
        checkpoint()
    finally:
        del connections[database]
        database.set_progress_handler(None, 0)
        database.set_trace_callback(None)
