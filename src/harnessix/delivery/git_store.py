from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_contracts import (
    GitCheckpoint,
    GitCommitRecord,
    GitCommitSpec,
    ManagedGitWorktreePlan,
    ManagedGitWorktreeRecord,
    new_commit_record,
    new_worktree_record,
)

_SCHEMA_VERSION = "1"
_WORKTREE_TRANSITIONS = {
    "prepared": frozenset({"creating", "diverged", "unknown"}),
    "creating": frozenset({"ready", "diverged", "unknown"}),
    "ready": frozenset(),
    "diverged": frozenset(),
    "unknown": frozenset(),
}
_COMMIT_TRANSITIONS = {
    "prepared": frozenset({"committing", "diverged", "unknown"}),
    "committing": frozenset({"interrupted", "committed", "diverged", "unknown"}),
    "interrupted": frozenset({"committing", "committed", "diverged", "unknown"}),
    "committed": frozenset(),
    "diverged": frozenset(),
    "unknown": frozenset(),
}


class SQLiteGitDeliveryStore:
    """Git worktree、checkpoint和commit的私有持久账本。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._closed = False
        self._prepare_directory(self._root)
        self._path = self._root / "git-delivery.db"
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

    @property
    def root(self) -> Path:
        return self._root

    def _initialize(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS git_delivery_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS git_worktrees (
                worktree_id TEXT PRIMARY KEY,
                transaction_id TEXT NOT NULL UNIQUE,
                plan_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                payload TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS git_worktree_events (
                worktree_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                state TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(worktree_id, sequence),
                FOREIGN KEY(worktree_id) REFERENCES git_worktrees(worktree_id)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS git_checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                worktree_id TEXT NOT NULL UNIQUE,
                transaction_id TEXT NOT NULL UNIQUE,
                digest TEXT NOT NULL,
                payload TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS git_commits (
                commit_id TEXT PRIMARY KEY,
                checkpoint_id TEXT NOT NULL UNIQUE,
                branch_ref TEXT NOT NULL UNIQUE,
                spec_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                payload TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS git_commit_events (
                commit_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                state TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(commit_id, sequence),
                FOREIGN KEY(commit_id) REFERENCES git_commits(commit_id)
            ) STRICT;
            """
        )
        row = self._db.execute(
            "SELECT value FROM git_delivery_metadata WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO git_delivery_metadata VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
        elif row != (_SCHEMA_VERSION,):
            raise KernelError("git_delivery_store_version", "Git交付存储版本不受支持")

    def save_worktree(self, plan: ManagedGitWorktreePlan) -> ManagedGitWorktreeRecord:
        record = new_worktree_record(plan)
        try:
            existing = self.load_worktree(plan.worktree_id)
        except KernelError as error:
            if error.code != "git_worktree_not_found":
                raise
        else:
            if existing.plan != plan:
                raise KernelError("git_worktree_conflict", "Git Worktree ID已绑定其他计划")
            return existing
        payload = record.model_dump_json(warnings="error")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "INSERT INTO git_worktrees VALUES (?,?,?,?,?,?)",
                (
                    str(record.worktree_id),
                    str(plan.transaction_id),
                    plan.fingerprint,
                    record.state,
                    record.sequence,
                    payload,
                ),
            )
            self._db.execute(
                "INSERT INTO git_worktree_events VALUES (?,?,?,?)",
                (str(record.worktree_id), record.sequence, record.state, payload),
            )
            self._db.execute("COMMIT")
        except sqlite3.IntegrityError:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise KernelError("git_worktree_conflict", "Git Worktree计划冲突") from None
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return record

    def transition_worktree(
        self, current: ManagedGitWorktreeRecord, updated: ManagedGitWorktreeRecord
    ) -> None:
        before = self._validate_worktree(current)
        after = self._validate_worktree(updated)
        if (
            before.plan != after.plan
            or before.worktree_id != after.worktree_id
            or after.sequence != before.sequence + 1
            or after.state not in _WORKTREE_TRANSITIONS[before.state]
        ):
            raise KernelError("git_worktree_transition_invalid", "Git Worktree状态迁移无效")
        self._transition(
            table="git_worktrees",
            event_table="git_worktree_events",
            identity_column="worktree_id",
            identity=str(before.worktree_id),
            current_state=before.state,
            current_sequence=before.sequence,
            current_payload=before.model_dump_json(warnings="error"),
            updated_state=after.state,
            updated_sequence=after.sequence,
            updated_payload=after.model_dump_json(warnings="error"),
            stale_code="git_worktree_stale",
        )

    def load_worktree(self, worktree_id: UUID) -> ManagedGitWorktreeRecord:
        row = self._db.execute(
            "SELECT worktree_id, transaction_id, plan_fingerprint, state, sequence, payload "
            "FROM git_worktrees WHERE worktree_id=?",
            (str(worktree_id),),
        ).fetchone()
        if row is None:
            raise KernelError("git_worktree_not_found", "Git Worktree记录不存在")
        try:
            record = ManagedGitWorktreeRecord.model_validate_json(row[5], strict=True)
            if row[:5] != (
                str(record.worktree_id),
                str(record.plan.transaction_id),
                record.plan.fingerprint,
                record.state,
                record.sequence,
            ):
                raise ValueError
            self._check_event(
                "git_worktree_events",
                "worktree_id",
                str(record.worktree_id),
                record.sequence,
                record.state,
                row[5],
            )
            return record
        except (ValidationError, ValueError, TypeError):
            raise KernelError("git_delivery_store_corrupt", "Git Worktree账本损坏") from None

    def save_checkpoint(self, checkpoint: GitCheckpoint) -> GitCheckpoint:
        payload = checkpoint.model_dump_json(warnings="error")
        try:
            self._db.execute(
                "INSERT INTO git_checkpoints VALUES (?,?,?,?,?)",
                (
                    str(checkpoint.checkpoint_id),
                    str(checkpoint.worktree_id),
                    str(checkpoint.transaction_id),
                    checkpoint.digest,
                    payload,
                ),
            )
        except sqlite3.IntegrityError:
            row = self._db.execute(
                "SELECT payload FROM git_checkpoints WHERE worktree_id=? OR transaction_id=?",
                (str(checkpoint.worktree_id), str(checkpoint.transaction_id)),
            ).fetchone()
            if row == (payload,):
                return checkpoint
            raise KernelError("git_checkpoint_conflict", "Git Checkpoint身份冲突") from None
        return checkpoint

    def load_checkpoint(self, checkpoint_id: UUID) -> GitCheckpoint:
        row = self._db.execute(
            "SELECT checkpoint_id, worktree_id, transaction_id, digest, payload "
            "FROM git_checkpoints WHERE checkpoint_id=?",
            (str(checkpoint_id),),
        ).fetchone()
        if row is None:
            raise KernelError("git_checkpoint_not_found", "Git Checkpoint不存在")
        try:
            checkpoint = GitCheckpoint.model_validate_json(row[4], strict=True)
            if row[:4] != (
                str(checkpoint.checkpoint_id),
                str(checkpoint.worktree_id),
                str(checkpoint.transaction_id),
                checkpoint.digest,
            ):
                raise ValueError
            return checkpoint
        except (ValidationError, ValueError, TypeError):
            raise KernelError("git_delivery_store_corrupt", "Git Checkpoint记录损坏") from None

    def checkpoint_for_worktree(self, worktree_id: UUID) -> GitCheckpoint | None:
        row = self._db.execute(
            "SELECT checkpoint_id FROM git_checkpoints WHERE worktree_id=?",
            (str(worktree_id),),
        ).fetchone()
        return None if row is None else self.load_checkpoint(UUID(str(row[0])))

    def save_commit(self, spec: GitCommitSpec) -> GitCommitRecord:
        record = new_commit_record(spec)
        try:
            existing = self.load_commit(spec.commit_id)
        except KernelError as error:
            if error.code != "git_commit_not_found":
                raise
        else:
            if existing.spec != spec:
                raise KernelError("git_commit_conflict", "Git Commit ID已绑定其他计划")
            return existing
        payload = record.model_dump_json(warnings="error")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "INSERT INTO git_commits VALUES (?,?,?,?,?,?,?)",
                (
                    str(record.commit_id),
                    str(spec.checkpoint.checkpoint_id),
                    spec.branch_ref,
                    spec.fingerprint,
                    record.state,
                    record.sequence,
                    payload,
                ),
            )
            self._db.execute(
                "INSERT INTO git_commit_events VALUES (?,?,?,?)",
                (str(record.commit_id), record.sequence, record.state, payload),
            )
            self._db.execute("COMMIT")
        except sqlite3.IntegrityError:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise KernelError("git_commit_conflict", "Git Commit计划冲突") from None
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return record

    def transition_commit(self, current: GitCommitRecord, updated: GitCommitRecord) -> None:
        before = self._validate_commit(current)
        after = self._validate_commit(updated)
        if (
            before.spec != after.spec
            or before.commit_id != after.commit_id
            or after.sequence != before.sequence + 1
            or after.state not in _COMMIT_TRANSITIONS[before.state]
        ):
            raise KernelError("git_commit_transition_invalid", "Git Commit状态迁移无效")
        self._transition(
            table="git_commits",
            event_table="git_commit_events",
            identity_column="commit_id",
            identity=str(before.commit_id),
            current_state=before.state,
            current_sequence=before.sequence,
            current_payload=before.model_dump_json(warnings="error"),
            updated_state=after.state,
            updated_sequence=after.sequence,
            updated_payload=after.model_dump_json(warnings="error"),
            stale_code="git_commit_stale",
        )

    def load_commit(self, commit_id: UUID) -> GitCommitRecord:
        row = self._db.execute(
            "SELECT commit_id, checkpoint_id, branch_ref, spec_fingerprint, "
            "state, sequence, payload "
            "FROM git_commits WHERE commit_id=?",
            (str(commit_id),),
        ).fetchone()
        if row is None:
            raise KernelError("git_commit_not_found", "Git Commit记录不存在")
        try:
            record = GitCommitRecord.model_validate_json(row[6], strict=True)
            if row[:6] != (
                str(record.commit_id),
                str(record.spec.checkpoint.checkpoint_id),
                record.spec.branch_ref,
                record.spec.fingerprint,
                record.state,
                record.sequence,
            ):
                raise ValueError
            self._check_event(
                "git_commit_events",
                "commit_id",
                str(record.commit_id),
                record.sequence,
                record.state,
                row[6],
            )
            return record
        except (ValidationError, ValueError, TypeError):
            raise KernelError("git_delivery_store_corrupt", "Git Commit账本损坏") from None

    def _transition(
        self,
        *,
        table: str,
        event_table: str,
        identity_column: str,
        identity: str,
        current_state: str,
        current_sequence: int,
        current_payload: str,
        updated_state: str,
        updated_sequence: int,
        updated_payload: str,
        stale_code: str,
    ) -> None:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                f"SELECT state, sequence, payload FROM {table} WHERE {identity_column}=?",
                (identity,),
            ).fetchone()
            if row != (current_state, current_sequence, current_payload):
                raise KernelError(stale_code, "Git交付记录已由其他owner推进")
            self._db.execute(
                f"UPDATE {table} SET state=?, sequence=?, payload=? "
                f"WHERE {identity_column}=? AND sequence=?",
                (updated_state, updated_sequence, updated_payload, identity, current_sequence),
            )
            self._db.execute(
                f"INSERT INTO {event_table} VALUES (?,?,?,?)",
                (identity, updated_sequence, updated_state, updated_payload),
            )
            self._db.execute("COMMIT")
        except sqlite3.IntegrityError:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise KernelError(stale_code, "Git交付事件序号冲突") from None
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _check_event(
        self,
        table: str,
        identity_column: str,
        identity: str,
        sequence: int,
        state: str,
        payload: object,
    ) -> None:
        event = self._db.execute(
            f"SELECT state, payload FROM {table} WHERE {identity_column}=? AND sequence=?",
            (identity, sequence),
        ).fetchone()
        count = self._db.execute(
            f"SELECT count(*) FROM {table} WHERE {identity_column}=?", (identity,)
        ).fetchone()
        if event != (state, payload) or count != (sequence + 1,):
            raise ValueError

    @staticmethod
    def _validate_worktree(record: ManagedGitWorktreeRecord) -> ManagedGitWorktreeRecord:
        try:
            return ManagedGitWorktreeRecord.model_validate_json(
                record.model_dump_json(warnings="error"), strict=True
            )
        except (ValidationError, ValueError, TypeError):
            raise KernelError("git_worktree_record_invalid", "Git Worktree记录无效") from None

    @staticmethod
    def _validate_commit(record: GitCommitRecord) -> GitCommitRecord:
        try:
            return GitCommitRecord.model_validate_json(
                record.model_dump_json(warnings="error"), strict=True
            )
        except (ValidationError, ValueError, TypeError):
            raise KernelError("git_commit_record_invalid", "Git Commit记录无效") from None

    @staticmethod
    def _prepare_directory(path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OSError
            if os.name == "posix":
                path.chmod(0o700)
                info = path.stat()
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise OSError
        except OSError:
            raise KernelError("git_delivery_store_invalid", "Git交付私有目录无效") from None

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteGitDeliveryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
