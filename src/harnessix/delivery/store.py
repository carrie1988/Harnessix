"""以SQLite事件链和CAS持久化Workspace事务，并执行数据库耐久性检查。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import (
    MAX_TRANSACTION_FILE_BYTES,
    TransactionState,
    WorkspaceTransactionRecord,
    new_transaction_record,
)
from harnessix.delivery.planner import PreparedWorkspaceTransaction
from harnessix.delivery.workspace_cas_io import confirm_blob_durable, read_blob_body
from harnessix.delivery.workspace_record_codec import (
    DecodedWorkspaceRecord,
    decode_workspace_record,
    encode_workspace_record,
    validate_workspace_record,
)
from harnessix.delivery.workspace_store_schema import (
    admit_workspace_record_v2,
    check_workspace_store_schema,
    initialize_workspace_store,
)
from harnessix.delivery.workspace_v2_contracts import WorkspaceTransactionRecordV2
from harnessix.sqlite_readonly import readonly_database
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.snapshot_ports import (
    WorkspacePureProgressFactory,
    observed_workspace_pure_progress,
)
from harnessix.workspace.terminal_read_control import (
    require_store_write_allowed,
    run_store_read_checkpoint,
)

_TRANSITIONS: dict[TransactionState, frozenset[TransactionState]] = {
    "prepared": frozenset({"publishing", "diverged", "unknown"}),
    "publishing": frozenset({"publishing", "interrupted", "published", "diverged", "unknown"}),
    "interrupted": frozenset({"publishing", "published", "diverged", "unknown"}),
    "published": frozenset(),
    "diverged": frozenset(),
    "unknown": frozenset(),
}


class SQLiteWorkspaceTransactionStore:
    """私有CAS与append-only事务账本；文件正文不进入公开Plan。"""

    def __init__(
        self,
        root: str | Path,
        *,
        read_only: bool = False,
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        self._root = Path(root)
        self._closed = False
        self._read_only = read_only
        self._checkpoint = checkpoint
        self._blobs = self._root / "blobs"
        self._path = self._root / "transactions.db"
        if read_only:
            self._db = readonly_database(self._path)
            try:
                check_workspace_store_schema(self._db)
            except BaseException:
                self._db.close()
                self._closed = True
                raise
            return
        _prepare_directory(self._root)
        _prepare_directory(self._blobs)
        self._db = sqlite3.connect(self._path, isolation_level=None, timeout=5)
        try:
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            if os.name == "posix":
                self._path.chmod(0o600)
            initialize_workspace_store(self._db)
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    def save(self, prepared: PreparedWorkspaceTransaction) -> WorkspaceTransactionRecord:
        self._require_writable()
        self._check()
        for digest, body in sorted(prepared.blobs.items()):
            self._put_blob(digest, body)
        record = new_transaction_record(prepared.plan)
        existing = self.lookup(record.plan.request_id)
        if existing is not None:
            if existing.transaction_id != record.transaction_id or existing.plan != record.plan:
                raise KernelError("delivery_request_conflict", "Workspace事务请求已绑定其他计划")
            return existing
        payload = self._encode(record)
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._admit(record)
            current = self._db.execute(
                "SELECT request_id, plan_fingerprint, state, sequence, payload "
                "FROM workspace_transactions WHERE transaction_id=?",
                (str(record.transaction_id),),
            ).fetchone()
            expected = (
                record.plan.request_id,
                record.plan.fingerprint,
                record.state,
                record.sequence,
                payload,
            )
            if current is None:
                self._db.execute(
                    "INSERT INTO workspace_transactions VALUES (?,?,?,?,?,?)",
                    (str(record.transaction_id), *expected),
                )
                self._db.execute(
                    "INSERT INTO workspace_transaction_events VALUES (?,?,?,?)",
                    (str(record.transaction_id), record.sequence, record.state, payload),
                )
            elif current != expected:
                raise KernelError("delivery_transaction_conflict", "事务身份已绑定其他计划")
            self._check()
            self._db.execute("COMMIT")
        except sqlite3.IntegrityError:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise KernelError("delivery_request_conflict", "Workspace事务请求冲突") from None
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return record

    def transition(
        self, current: WorkspaceTransactionRecord, updated: WorkspaceTransactionRecord
    ) -> None:
        self._require_writable()
        self._check()
        before = self._validate(current)
        after = self._validate(updated)
        if (
            after.transaction_id != before.transaction_id
            or after.plan != before.plan
            or after.sequence != before.sequence + 1
            or after.state not in _TRANSITIONS[before.state]
            or after.cursor < before.cursor
        ):
            raise KernelError("delivery_transition_invalid", "Workspace事务状态迁移无效")
        payload = self._encode(after)
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._admit(after)
            stored = self._db.execute(
                "SELECT state, sequence, payload FROM workspace_transactions "
                "WHERE transaction_id=?",
                (str(before.transaction_id),),
            ).fetchone()
            if (
                stored is None
                or stored[:2] != (before.state, before.sequence)
                or self.decode_payload(stored[2]).record != before
            ):
                raise KernelError("delivery_transaction_stale", "Workspace事务已由其他owner推进")
            self._db.execute(
                "UPDATE workspace_transactions SET state=?, sequence=?, payload=? "
                "WHERE transaction_id=? AND sequence=? AND payload=?",
                (
                    after.state,
                    after.sequence,
                    payload,
                    str(after.transaction_id),
                    before.sequence,
                    stored[2],
                ),
            )
            self._db.execute(
                "INSERT INTO workspace_transaction_events VALUES (?,?,?,?)",
                (str(after.transaction_id), after.sequence, after.state, payload),
            )
            self._check()
            self._db.execute("COMMIT")
        except sqlite3.IntegrityError:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise KernelError("delivery_transaction_stale", "Workspace事务事件序号冲突") from None
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def load(
        self,
        transaction_id: UUID,
        *,
        checkpoint: Callable[[], None] | None = None,
        pure_progress: WorkspacePureProgressFactory | None = None,
    ) -> WorkspaceTransactionRecord:
        if checkpoint is not None:
            checkpoint()
        row = self._db.execute(
            "SELECT transaction_id, request_id, plan_fingerprint, state, sequence, payload "
            "FROM workspace_transactions WHERE transaction_id=?",
            (str(transaction_id),),
        ).fetchone()
        if row is None:
            raise KernelError("delivery_transaction_not_found", "Workspace事务不存在")
        if pure_progress is None:
            return self._decode(row, checkpoint=checkpoint)
        return self._decode(row, checkpoint=checkpoint, pure_progress=pure_progress)

    def lookup(self, request_id: str) -> WorkspaceTransactionRecord | None:
        row = self._db.execute(
            "SELECT transaction_id, request_id, plan_fingerprint, state, sequence, payload "
            "FROM workspace_transactions WHERE request_id=?",
            (request_id,),
        ).fetchone()
        return None if row is None else self._decode(row)

    def blob(self, digest: str, *, checkpoint: Callable[[], None] | None = None) -> bytes:
        """完整回读；显式操作检查点仅以原控制标记传播，不改变共享回调。"""
        check = _blob_checkpoint(self._check, checkpoint)
        check()
        body = self._read_blob(digest)
        check()
        return body

    def _read_blob(self, digest: str) -> bytes:
        """共用原CAS严格IO；Plan元数据读取不触发文件镜像端口。"""
        if not _valid_digest(digest):
            raise KernelError("delivery_blob_invalid", "Workspace事务Blob摘要无效")
        return read_blob_body(self._blobs / digest, digest)

    def decode_payload(
        self,
        payload: str,
        *,
        checkpoint: Callable[[], None] | None = None,
        pure_progress: WorkspacePureProgressFactory | None = None,
    ) -> DecodedWorkspaceRecord:
        """完整读取当前行或历史物理记录；校验引用不授予执行、迁移或补签权。"""

        def check() -> None:
            self._check()
            if checkpoint is not None:
                checkpoint()

        if pure_progress is None:
            return decode_workspace_record(payload, self._read_blob, checkpoint=check)
        return decode_workspace_record(
            payload,
            self._read_blob,
            checkpoint=check,
            pure_progress=observed_workspace_pure_progress(pure_progress, self._check),
        )

    def _encode(self, record: WorkspaceTransactionRecord) -> str:
        return encode_workspace_record(
            record, self.put_blob, read_blob=self._read_blob, checkpoint=self._checkpoint
        )

    def _admit(self, record: WorkspaceTransactionRecord) -> None:
        if isinstance(record, WorkspaceTransactionRecordV2):
            admit_workspace_record_v2(self._db)

    def _check(self) -> None:
        run_store_read_checkpoint(self, self._checkpoint)

    def _require_writable(self) -> None:
        """只读权限在文件副作用与输入解析之前检查，不只依赖 SQLite 拒绝。"""
        require_store_write_allowed(self)
        if self._read_only:
            raise KernelError("delivery_store_read_only", "Workspace事务只读账本不接受写入")
        if self._closed:
            raise KernelError("delivery_store_closed", "Workspace事务账本已关闭")

    def put_blob(
        self, digest: str, body: bytes, *, checkpoint: Callable[[], None] | None = None
    ) -> None:
        """原 CAS 耐久写；显式检查点保留控制标记，默认仍解包为原异常。"""
        self._require_writable()
        check = _blob_checkpoint(self._check, checkpoint)
        check()
        if checkpoint is None:
            self._put_blob(digest, body)
        else:
            self._put_blob(digest, body, checkpoint=checkpoint)
        read = (
            (lambda: _controlled_io(lambda: self.blob(digest)))
            if checkpoint is None
            else (lambda: self.blob(digest, checkpoint=checkpoint))
        )
        try:
            confirm_blob_durable(
                self._blobs / digest,
                body,
                read,
                _fsync_directory,
            )
        except UpstreamCheckpointError as error:
            if checkpoint is not None:
                raise
            raise error.error from None
        check()

    def _put_blob(
        self, digest: str, body: bytes, *, checkpoint: Callable[[], None] | None = None
    ) -> None:
        self._require_writable()
        read = (
            self.blob
            if checkpoint is None
            else lambda digest: self.blob(digest, checkpoint=checkpoint)
        )
        _write_blob_body(self._blobs, digest, body, read, _blob_checkpoint(self._check, checkpoint))

    def _decode(
        self,
        row: tuple[object, ...],
        *,
        checkpoint: Callable[[], None] | None = None,
        pure_progress: WorkspacePureProgressFactory | None = None,
    ) -> WorkspaceTransactionRecord:
        if not isinstance(row[5], str):
            raise KernelError("delivery_store_corrupt", "Workspace事务账本损坏")
        record = (
            self.decode_payload(row[5], checkpoint=checkpoint)
            if pure_progress is None
            else self.decode_payload(row[5], checkpoint=checkpoint, pure_progress=pure_progress)
        ).record
        try:
            if row[:5] != (
                str(record.transaction_id),
                record.plan.request_id,
                record.plan.fingerprint,
                record.state,
                record.sequence,
            ):
                raise ValueError
            event = self._db.execute(
                "SELECT state, payload FROM workspace_transaction_events "
                "WHERE transaction_id=? AND sequence=?",
                (str(record.transaction_id), record.sequence),
            ).fetchone()
            count = self._db.execute(
                "SELECT count(*) FROM workspace_transaction_events WHERE transaction_id=?",
                (str(record.transaction_id),),
            ).fetchone()
            if event != (record.state, row[5]) or count != (record.sequence + 1,):
                raise ValueError
            return record
        except (ValidationError, ValueError, TypeError):
            raise KernelError("delivery_store_corrupt", "Workspace事务账本损坏") from None

    @staticmethod
    def _validate(record: WorkspaceTransactionRecord) -> WorkspaceTransactionRecord:
        return validate_workspace_record(record)

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteWorkspaceTransactionStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _blob_checkpoint(
    original: Callable[[], None], checkpoint: Callable[[], None] | None
) -> Callable[[], None]:
    """显式操作先消费原回调再消费调用方；仅实际检查点异常标为控制信号。"""
    if checkpoint is None:
        return original

    def check() -> None:
        try:
            original()
            checkpoint()
        except BaseException as error:
            raise UpstreamCheckpointError(error) from None

    return check


def _controlled_io[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except BaseException as error:
        raise UpstreamCheckpointError(error) from None


def _write_blob_body(
    blobs: Path,
    digest: str,
    body: bytes,
    read_blob: Callable[[str], bytes],
    checkpoint: Callable[[], None],
) -> None:
    """原 CAS 写入算法及限额不变；控制异常穿过文件错误转换边界。"""
    if (
        not _valid_digest(digest)
        or type(body) is not bytes
        or len(body) > MAX_TRANSACTION_FILE_BYTES
        or hashlib.sha256(body).hexdigest() != digest
    ):
        raise KernelError("delivery_blob_invalid", "Workspace事务Blob与摘要不一致")
    target = blobs / digest
    if target.exists():
        if read_blob(digest) != body:
            raise KernelError("delivery_blob_conflict", "Workspace事务Blob发生冲突")
        return
    temporary = blobs / f".{digest}.{uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor: int | None = None
    temporary_owned = False
    try:
        descriptor = os.open(temporary, flags, 0o600)
        temporary_owned = True
        offset = 0
        while offset < len(body):
            _controlled_io(checkpoint)
            written = os.write(descriptor, body[offset : offset + 65_536])
            if written <= 0:
                raise OSError
            offset += written
        _controlled_io(checkpoint)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, target)
        _fsync_directory(blobs)
        if _controlled_io(lambda: read_blob(digest)) != body:
            raise OSError
    except UpstreamCheckpointError as error:
        raise error.error from None
    except OSError:
        raise KernelError("delivery_storage_unavailable", "Workspace事务Blob写入失败") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        # O_EXCL 成功才拥有清理权；创建失败时不能删除同名陌生文件。
        if temporary_owned:
            try:
                temporary.unlink()
            except OSError:
                pass


def _valid_digest(value: str) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_directory(path: Path) -> None:
    try:
        if os.name == "nt":
            from harnessix.workspace.windows_private_directory import private_state_directory

            private_state_directory(path, parents=True, exist_ok=True)
        else:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            raise OSError
        if os.name == "posix":
            path.chmod(0o700)
            info = path.stat(follow_symlinks=False)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise OSError
    except (OSError, KernelError):
        raise KernelError("delivery_store_invalid", "Workspace事务私有目录无效") from None
