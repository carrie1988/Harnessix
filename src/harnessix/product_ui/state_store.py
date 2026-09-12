"""客户端恢复元数据的单写者存储与领域变更。"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from harnessix.product_ui.contracts import (
    MAX_CLIENT_THREAD_CURSORS,
    MAX_SAFE_JSON_INTEGER,
    ClientCommandAllocation,
    ClientStateV1,
    ClientThreadCursor,
    command_request_id,
    evolve_client_state,
    new_client_state,
    workspace_identity_fingerprint,
)
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.state_file import (
    open_state_lock,
    prepare_private_directory,
    read_state_file,
    write_state_file,
)


def _allocate_command(state: ClientStateV1) -> tuple[ClientStateV1, ClientCommandAllocation]:
    if state.next_command_sequence >= MAX_SAFE_JSON_INTEGER:
        raise ProductUIError("client_command_exhausted", "客户端命令序列已耗尽")
    sequence = state.next_command_sequence
    updated = evolve_client_state(state, next_command_sequence=sequence + 1)
    return updated, ClientCommandAllocation(
        request_id=command_request_id(state.client_instance_id, sequence),
        sequence=sequence,
        state_revision=updated.state_revision,
    )


def _advance_cursor(state: ClientStateV1, thread_id: UUID, cursor: int) -> ClientStateV1:
    if cursor <= state.cursor_for(thread_id):
        return state
    by_thread = {item.thread_id: item.cursor for item in state.thread_cursors}
    if thread_id not in by_thread and len(by_thread) >= MAX_CLIENT_THREAD_CURSORS:
        raise ProductUIError("client_state_limit", "Thread Cursor数量超过上限")
    by_thread[thread_id] = cursor
    cursors = tuple(
        ClientThreadCursor(thread_id=identity, cursor=value)
        for identity, value in sorted(by_thread.items(), key=lambda item: str(item[0]))
    )
    return evolve_client_state(state, thread_cursors=cursors)


class ClientStateStore:
    """持有状态目录排他锁，并在每次变更前重读磁盘事实。"""

    def __init__(self, state_directory: str | Path, *, workspace_identity: str) -> None:
        try:
            fingerprint = workspace_identity_fingerprint(workspace_identity)
        except (TypeError, UnicodeError, ValueError):
            raise ProductUIError("client_state_workspace_invalid", "Workspace身份无效") from None
        self._root = Path(state_directory).absolute()
        self._state_path = self._root / "client-state.json"
        self._lock_descriptor: int | None = None
        self._workspace_fingerprint = fingerprint
        self._closed = False
        self._mutex = threading.RLock()
        try:
            prepare_private_directory(self._root)
            self._lock_descriptor = open_state_lock(self._root / ".client-state.lock")
            if self._state_path.exists() or self._state_path.is_symlink():
                self._read()
            else:
                write_state_file(self._root, self._state_path, new_client_state(fingerprint))
        except BaseException:
            self._release_lock()
            self._closed = True
            raise

    def _read(self) -> ClientStateV1:
        return read_state_file(self._state_path, self._workspace_fingerprint)

    def _require_open(self) -> None:
        if self._closed:
            raise ProductUIError("client_state_closed", "客户端状态存储已关闭")

    def state(self) -> ClientStateV1:
        with self._mutex:
            self._require_open()
            return self._read()

    def _mutate(self, operation: Callable[[ClientStateV1], ClientStateV1]) -> ClientStateV1:
        with self._mutex:
            self._require_open()
            current = self._read()
            try:
                updated = operation(current)
            except ProductUIError:
                raise
            except (TypeError, ValueError):
                raise ProductUIError("client_state_invalid", "客户端状态变更不符合合同") from None
            if updated != current:
                write_state_file(self._root, self._state_path, updated)
            return updated

    def allocate_command_id(self) -> ClientCommandAllocation:
        allocation: ClientCommandAllocation | None = None

        def allocate(state: ClientStateV1) -> ClientStateV1:
            nonlocal allocation
            updated, allocation = _allocate_command(state)
            return updated

        self._mutate(allocate)
        assert allocation is not None
        return allocation

    def select_thread(self, thread_id: UUID | None) -> ClientStateV1:
        if thread_id is not None and not isinstance(thread_id, UUID):
            raise ProductUIError("client_state_invalid", "Thread ID无效")
        return self._mutate(
            lambda state: (
                state
                if state.selected_thread_id == thread_id
                else evolve_client_state(state, selected_thread_id=thread_id)
            )
        )

    def advance_cursor(self, thread_id: UUID, cursor: int) -> ClientStateV1:
        if not isinstance(thread_id, UUID) or type(cursor) is not int or cursor < 0:
            raise ProductUIError("client_state_invalid", "Thread Cursor无效")
        return self._mutate(lambda state: _advance_cursor(state, thread_id, cursor))

    def forget_thread_cursor(self, thread_id: UUID) -> ClientStateV1:
        if not isinstance(thread_id, UUID):
            raise ProductUIError("client_state_invalid", "Thread ID无效")

        def forget(state: ClientStateV1) -> ClientStateV1:
            cursors = tuple(item for item in state.thread_cursors if item.thread_id != thread_id)
            return (
                state
                if cursors == state.thread_cursors
                else evolve_client_state(state, thread_cursors=cursors)
            )

        return self._mutate(forget)

    def set_clean_shutdown(self, clean: bool) -> ClientStateV1:
        if type(clean) is not bool:
            raise ProductUIError("client_state_invalid", "安全关闭标志无效")
        return self._mutate(
            lambda state: (
                state
                if state.clean_shutdown == clean
                else evolve_client_state(state, clean_shutdown=clean)
            )
        )

    def _release_lock(self) -> None:
        if self._lock_descriptor is not None:
            try:
                os.close(self._lock_descriptor)
            finally:
                self._lock_descriptor = None

    def close(self) -> None:
        with self._mutex:
            if not self._closed:
                self._release_lock()
                self._closed = True

    def __enter__(self) -> ClientStateStore:
        self._require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
