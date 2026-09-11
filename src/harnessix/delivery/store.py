"""以SQLite事件链和CAS持久化Workspace事务，并执行数据库耐久性检查。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
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

_SCHEMA_VERSION = "1"
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

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._closed = False
        self._prepare_directory(self._root)
        self._blobs = self._root / "blobs"
        self._prepare_directory(self._blobs)
        self._path = self._root / "transactions.db"
        self._db = sqlite3.connect(self._path, isolation_level=None, timeout=5)
        try:
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            if os.name == "posix":
                self._path.chmod(0o600)
            self._initialize()
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    def _initialize(self) -> None:
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS delivery_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
        )
        row = self._db.execute(
            "SELECT value FROM delivery_metadata WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO delivery_metadata VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
        elif row[0] != _SCHEMA_VERSION:
            raise KernelError("delivery_store_version", "Workspace事务存储版本不受支持")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspace_transactions (
                transaction_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                plan_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                payload TEXT NOT NULL
            ) STRICT;
            CREATE INDEX IF NOT EXISTS workspace_transactions_state
                ON workspace_transactions(state);
            CREATE TABLE IF NOT EXISTS workspace_transaction_events (
                transaction_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                state TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(transaction_id, sequence),
                FOREIGN KEY(transaction_id) REFERENCES workspace_transactions(transaction_id)
            ) STRICT;
            """
        )

    def save(self, prepared: PreparedWorkspaceTransaction) -> WorkspaceTransactionRecord:
        for digest, body in sorted(prepared.blobs.items()):
            self._put_blob(digest, body)
        record = new_transaction_record(prepared.plan)
        existing = self.lookup(record.plan.request_id)
        if existing is not None:
            if existing.transaction_id != record.transaction_id or existing.plan != record.plan:
                raise KernelError("delivery_request_conflict", "Workspace事务请求已绑定其他计划")
            return existing
        payload = record.model_dump_json(warnings="error")
        try:
            self._db.execute("BEGIN IMMEDIATE")
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
        payload = after.model_dump_json(warnings="error")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            stored = self._db.execute(
                "SELECT state, sequence, payload FROM workspace_transactions "
                "WHERE transaction_id=?",
                (str(before.transaction_id),),
            ).fetchone()
            if stored != (
                before.state,
                before.sequence,
                before.model_dump_json(warnings="error"),
            ):
                raise KernelError("delivery_transaction_stale", "Workspace事务已由其他owner推进")
            self._db.execute(
                "UPDATE workspace_transactions SET state=?, sequence=?, payload=? "
                "WHERE transaction_id=? AND sequence=?",
                (
                    after.state,
                    after.sequence,
                    payload,
                    str(after.transaction_id),
                    before.sequence,
                ),
            )
            self._db.execute(
                "INSERT INTO workspace_transaction_events VALUES (?,?,?,?)",
                (str(after.transaction_id), after.sequence, after.state, payload),
            )
            self._db.execute("COMMIT")
        except sqlite3.IntegrityError:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise KernelError("delivery_transaction_stale", "Workspace事务事件序号冲突") from None
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def load(self, transaction_id: UUID) -> WorkspaceTransactionRecord:
        row = self._db.execute(
            "SELECT transaction_id, request_id, plan_fingerprint, state, sequence, payload "
            "FROM workspace_transactions WHERE transaction_id=?",
            (str(transaction_id),),
        ).fetchone()
        if row is None:
            raise KernelError("delivery_transaction_not_found", "Workspace事务不存在")
        return self._decode(row)

    def lookup(self, request_id: str) -> WorkspaceTransactionRecord | None:
        row = self._db.execute(
            "SELECT transaction_id, request_id, plan_fingerprint, state, sequence, payload "
            "FROM workspace_transactions WHERE request_id=?",
            (request_id,),
        ).fetchone()
        return None if row is None else self._decode(row)

    def blob(self, digest: str) -> bytes:
        if not _valid_digest(digest):
            raise KernelError("delivery_blob_invalid", "Workspace事务Blob摘要无效")
        path = self._blobs / digest
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or not 0 <= info.st_size <= MAX_TRANSACTION_FILE_BYTES
            ):
                raise OSError
            if os.name == "posix" and (
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise OSError
            body = bytearray()
            while len(body) <= MAX_TRANSACTION_FILE_BYTES:
                chunk = os.read(descriptor, min(65_536, MAX_TRANSACTION_FILE_BYTES + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            result = bytes(body)
            if len(result) != info.st_size or hashlib.sha256(result).hexdigest() != digest:
                raise OSError
            return result
        except OSError:
            raise KernelError("delivery_blob_corrupt", "Workspace事务Blob损坏或缺失") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _put_blob(self, digest: str, body: bytes) -> None:
        if (
            not _valid_digest(digest)
            or type(body) is not bytes
            or len(body) > MAX_TRANSACTION_FILE_BYTES
            or hashlib.sha256(body).hexdigest() != digest
        ):
            raise KernelError("delivery_blob_invalid", "Workspace事务Blob与摘要不一致")
        target = self._blobs / digest
        if target.exists():
            if self.blob(digest) != body:
                raise KernelError("delivery_blob_conflict", "Workspace事务Blob发生冲突")
            return
        temporary = self._blobs / f".{digest}.{uuid4().hex}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            offset = 0
            while offset < len(body):
                written = os.write(descriptor, body[offset : offset + 65_536])
                if written <= 0:
                    raise OSError
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, target)
            _fsync_directory(self._blobs)
            if self.blob(digest) != body:
                raise OSError
        except OSError:
            raise KernelError("delivery_storage_unavailable", "Workspace事务Blob写入失败") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except OSError:
                pass

    def _decode(self, row: tuple[object, ...]) -> WorkspaceTransactionRecord:
        try:
            if not isinstance(row[5], str) or len(row[5].encode()) > 512 * 1024:
                raise ValueError
            record = WorkspaceTransactionRecord.model_validate_json(row[5], strict=True)
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
        try:
            return WorkspaceTransactionRecord.model_validate_json(
                record.model_dump_json(warnings="error"), strict=True
            )
        except (ValidationError, ValueError, TypeError):
            raise KernelError("delivery_record_invalid", "Workspace事务记录无效") from None

    @staticmethod
    def _prepare_directory(path: Path) -> None:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
                raise OSError
            if os.name == "posix":
                path.chmod(0o700)
                info = path.stat(follow_symlinks=False)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise OSError
        except OSError:
            raise KernelError("delivery_store_invalid", "Workspace事务私有目录无效") from None

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteWorkspaceTransactionStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


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
