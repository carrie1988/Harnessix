"""专用Git前缀连接的SQL合作中断与内部事务代际观察；不覆盖共享连接。"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from threading import local
from weakref import WeakValueDictionary

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_prepared_link_connection import (
    _current_task,
    _prepared_git_connection_registration_observer,
    _registered_prepared_connection,
)

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
        self.task = _current_task()
        self.prepared_source: object | None = None
        self.interrupted: BaseException | None = None
        self.epoch = 0
        self.windows: WeakValueDictionary[int, object] = WeakValueDictionary()

    def interrupt(self) -> int:
        try:
            self.checkpoint()
        except BaseException as error:
            if self.interrupted is None:
                self.interrupted = error
            return 1
        return 0


class _TransactionTrace:
    """长寿命原事务观察，仅保存实例代际与来源，不持有检查点或发布窗口。"""

    def __init__(self, prepared_source: object) -> None:
        self.prepared_source = prepared_source
        self.task = _current_task()
        self.epoch = 0


def _dispatch_git_prefix_trace(database: sqlite3.Connection, statement: str) -> None:
    """唯一合作式 trace 同时推进长观察和活动短窗口，不保存正文或执行宿主回调。"""
    if _transaction_token(statement) not in {
        "BEGIN",
        "COMMIT",
        "END",
        "ROLLBACK",
        "SAVEPOINT",
        "RELEASE",
    }:
        return
    for controls in (
        getattr(_owned, "transaction_traces", {}),
        getattr(_owned, "connections", {}),
    ):
        control = controls.get(database)
        if control is not None:
            control.epoch += 1


def _clear_git_prefix_callbacks(
    database: sqlite3.Connection, *, progress: bool, trace: bool
) -> None:
    """清理所有已安装部分；关闭已释放回调，清理错误不能遮盖原首失败。"""
    try:
        _ = database.in_transaction
    except sqlite3.ProgrammingError:
        return
    pending = sys.exception()
    first_error: BaseException | None = None
    callbacks: list[tuple[Callable[..., None], tuple[object, ...]]] = []
    if progress:
        callbacks.append((database.set_progress_handler, (None, 0)))
    if trace:
        callbacks.append((database.set_trace_callback, (None,)))
    for callback, arguments in callbacks:
        try:
            callback(*arguments)
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is not None and pending is None:
        raise first_error


@contextmanager
def _git_prefix_transaction_scope(database: sqlite3.Connection) -> Iterator[None]:
    """Runtime 在原 factory 连接上持有长观察；不授权 SQL，不控制调用方事务。

    必须由原 Task 安装，禁止覆盖已有长观察或短窗口。宿主覆盖 trace 属于既有
    合作边界，不提供 authorizer 拦截或恶意同进程隔离。
    """
    source = _registered_prepared_connection(database)
    if source is None:
        raise KernelError("git_prefix_sql_owner_invalid", "Git前缀事务观察需要原专用连接")
    traces = getattr(_owned, "transaction_traces", None)
    if traces is None:
        traces = {}
        _owned.transaction_traces = traces
    if database in traces or database in getattr(_owned, "connections", {}):
        raise KernelError("git_prefix_sql_control_conflict", "Git前缀连接已有事务观察或SQL窗口")
    traces[database] = _TransactionTrace(source)
    try:
        database.set_trace_callback(partial(_dispatch_git_prefix_trace, database))
        yield
    finally:
        del traces[database]
        _clear_git_prefix_callbacks(
            database, progress=False, trace=database not in getattr(_owned, "connections", {})
        )


def _git_prefix_caller_transaction_epoch(database: sqlite3.Connection) -> tuple[object, int]:
    """短同步读原长观察实例与事务代际；不执行 Owner 或其他外部回调。"""
    trace = getattr(_owned, "transaction_traces", {}).get(database)
    if trace is None:
        raise KernelError("publication_history_unproven", "Git原事务观察已经变化")
    if _current_task() is not trace.task or _registered_prepared_connection(database) is not (
        trace.prepared_source
    ):
        raise KernelError("git_prefix_sql_owner_invalid", "Git前缀事务观察原任务或来源已经变化")
    try:
        in_transaction = database.in_transaction
    except sqlite3.ProgrammingError:
        in_transaction = False
    if not in_transaction:
        raise KernelError("publication_history_unproven", "Git原事务代际已经变化")
    return trace, trace.epoch


def _require_git_prefix_caller_transaction_epoch(
    database: sqlite3.Connection, expected: tuple[object, int]
) -> None:
    """只比对长观察原实例及代际，不把观察令牌转换为 SQL 或发布权限。"""
    trace, epoch = _git_prefix_caller_transaction_epoch(database)
    if trace is not expected[0] or epoch != expected[1]:
        raise KernelError("publication_history_unproven", "Git原事务代际已经变化")


def _git_prefix_caller_transaction_observer(database: sqlite3.Connection) -> Callable[[], None]:
    """原 Task 签发只读观察；子 Task 可比较原代际，但不能新开 SQL 窗口。"""
    trace, epoch = _git_prefix_caller_transaction_epoch(database)
    if not isinstance(trace, _TransactionTrace):
        raise KernelError("publication_history_unproven", "Git原事务观察已经变化")
    observe_registration = _prepared_git_connection_registration_observer(database)

    def observe() -> None:
        observe_registration()
        current: object = getattr(_owned, "transaction_traces", {}).get(database)
        if current is not trace:
            raise KernelError("publication_history_unproven", "Git原事务观察已经变化")
        if trace.epoch != epoch or not database.in_transaction:
            raise KernelError("publication_history_unproven", "Git原事务代际已经变化")

    return observe


def require_git_prefix_sql_window(database: sqlite3.Connection) -> None:
    """有限端口不接受没有SQL取消窗口的原始连接。"""
    control = getattr(_owned, "connections", {}).get(database)
    if control is None:
        raise KernelError("git_prefix_sql_control_required", "Git前缀需要专用SQL检查点窗口")
    _require_control_owner(database, control)
    # 不能只依赖1000步回调；小表和await之后仍须立即消费原Owner/Scope/绝对期限。
    control.checkpoint()
    # 上游检查点可正常返回却已撤销连接或窗口；准入返回前再核对原来源。
    _require_control_owner(database, control)


def _require_control_owner(database: sqlite3.Connection, control: _SQLCheckpoint) -> None:
    """首失败优先，核对同一原窗口及连接/任务来源，不执行新的上游回调。"""
    if control.interrupted is not None:
        raise control.interrupted
    if getattr(_owned, "connections", {}).get(database) is not control:
        raise KernelError("git_prefix_sql_owner_invalid", "Git前缀原SQL窗口已经变化")
    if _registered_prepared_connection(database) is not control.prepared_source:
        raise KernelError("git_prefix_sql_owner_invalid", "Git前缀原连接来源已经变化")
    # 通用同步窗口保留原线程合同；产品Ledger在原Task内创建窗口，必须精确匹配。
    if control.task is not None and _current_task() is not control.task:
        raise KernelError("git_prefix_sql_owner_invalid", "Git前缀SQL窗口不属于当前任务")


def git_prefix_transaction_epoch(database: sqlite3.Connection) -> tuple[object, int]:
    """窗口只在同一SQL控制实例与事务边界代际内有效，不持久化或充当Fence。"""
    require_git_prefix_sql_window(database)
    control = _owned.connections[database]
    return control, control.epoch


def require_git_prefix_transaction_epoch(
    database: sqlite3.Connection, expected: tuple[object, int]
) -> None:
    """只核对原控制实例及代际，不再次执行宿主回调，供回调后的末端核验使用。"""
    control = getattr(_owned, "connections", {}).get(database)
    if control is None or (control, control.epoch) != expected or not database.in_transaction:
        raise KernelError("publication_history_unproven", "Git原事务代际已经变化")
    if control.interrupted is not None:
        raise control.interrupted


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
    source = _registered_prepared_connection(database)
    checkpoint()
    if _registered_prepared_connection(database) is not source:
        raise KernelError("git_prefix_sql_owner_invalid", "Git前缀原连接来源已经变化")
    connections = getattr(_owned, "connections", None)
    if connections is None:
        connections = {}
        _owned.connections = connections
    if database in connections:
        raise KernelError("git_prefix_sql_control_conflict", "Git前缀连接已有SQL检查点窗口")
    control = _SQLCheckpoint(checkpoint)
    control.prepared_source = source
    connections[database] = control
    try:
        database.set_progress_handler(control.interrupt, 1000)
        if database not in getattr(_owned, "transaction_traces", {}):
            database.set_trace_callback(partial(_dispatch_git_prefix_trace, database))
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
        _clear_git_prefix_callbacks(
            database, progress=True, trace=database not in getattr(_owned, "transaction_traces", {})
        )
