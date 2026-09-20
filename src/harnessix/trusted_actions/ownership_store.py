"""可信Action所有权存储：实现跨进程宿主锁与持久Generation栅栏。"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.domain.file_lock import acquire_exclusive_file_lock
from harnessix.domain.models import utc_now
from harnessix.trusted_actions.recovery_contracts import (
    ActionRuntimeFence,
)


def initialize_action_owner_schema(database: sqlite3.Connection) -> None:
    """为旧Action Audit库补齐Owner代次元数据。"""

    database.execute("INSERT OR IGNORE INTO action_audit_metadata VALUES ('owner_generation', '0')")
    database.execute(
        "INSERT OR IGNORE INTO action_audit_metadata VALUES ('owner_token_sha256', ?)",
        ("0" * 64,),
    )


class ActionOwnerFenceMixin:
    """校验当前Runtime Owner代次，拒绝过期宿主提交。"""

    _db: sqlite3.Connection
    _require_runtime_owner: bool
    _runtime_fence: ActionRuntimeFence | None

    @staticmethod
    def _token_digest(token: str) -> str:
        import hashlib

        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _assert_runtime_owner(self) -> ActionRuntimeFence | None:
        fence = self._runtime_fence
        if fence is None:
            if self._require_runtime_owner:
                raise KernelError("action_runtime_owner_required", "Action写入需要活跃Runtime宿主")
            return None
        rows = dict(
            self._db.execute(
                "SELECT key, value FROM action_audit_metadata "
                "WHERE key IN ('owner_generation', 'owner_token_sha256')"
            ).fetchall()
        )
        if rows.get("owner_generation") != str(fence.generation) or rows.get(
            "owner_token_sha256"
        ) != self._token_digest(fence.token):
            raise KernelError("action_runtime_fence_lost", "Action Runtime所有权已经失效")
        return fence


class ActionOwnershipStoreMixin(ActionOwnerFenceMixin):
    """集中Action Runtime锁、Owner代次与宿主文件锁。"""

    _path: Path

    @contextmanager
    def runtime_owner(self) -> Iterator[ActionRuntimeFence]:
        """取得产品Action单宿主锁并递增持久Generation，旧Generation不能继续提交。"""

        if self._runtime_fence is not None:
            raise KernelError("action_runtime_open", "Action Runtime不能重复取得所有权")
        descriptor = self._open_runtime_lock()
        try:
            token = os.urandom(32).hex()
            acquired_at = utc_now()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT value FROM action_audit_metadata WHERE key = 'owner_generation'"
                ).fetchone()
                if row is None:
                    raise KernelError("action_audit_store_corrupt", "Action Owner代次缺失")
                generation = int(row[0]) + 1
                self._db.execute(
                    "UPDATE action_audit_metadata SET value = ? WHERE key = 'owner_generation'",
                    (str(generation),),
                )
                self._db.execute(
                    "UPDATE action_audit_metadata SET value = ? WHERE key = 'owner_token_sha256'",
                    (self._token_digest(token),),
                )
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            fence = ActionRuntimeFence(
                generation=generation,
                token=token,
                acquired_at=acquired_at,
            )
            self._runtime_fence = fence
            try:
                yield fence
            finally:
                if self._runtime_fence == fence:
                    self._runtime_fence = None
        finally:
            os.close(descriptor)

    @property
    def runtime_fence(self) -> ActionRuntimeFence | None:
        fence = self._runtime_fence
        return fence.model_copy(deep=True) if fence is not None else None

    def _open_runtime_lock(self) -> int:
        path = Path(str(self._path) + ".runtime.lock")
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            acquire_exclusive_file_lock(descriptor)
        except BlockingIOError as error:
            if "descriptor" in locals():
                os.close(descriptor)
            raise KernelError("action_runtime_busy", "Action Runtime已有活跃宿主") from error
        except OSError:
            if "descriptor" in locals():
                os.close(descriptor)
            raise KernelError(
                "action_runtime_owner_unavailable", "Action Runtime宿主锁不可用"
            ) from None
        return descriptor
