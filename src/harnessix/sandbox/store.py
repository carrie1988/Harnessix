from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.sandbox.contracts import ContainerSandboxProfile

_SCHEMA_VERSION = "1"


class SQLiteSandboxProfileStore:
    """按内容摘要持久化不可变Sandbox Profile，不存储Secret或宿主路径。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._closed = False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self._path.parent.chmod(0o700)
        self._db = sqlite3.connect(self._path, isolation_level=None, timeout=5)
        try:
            self._db.execute("PRAGMA busy_timeout = 5000")
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
            "CREATE TABLE IF NOT EXISTS sandbox_store_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
        )
        row = self._db.execute(
            "SELECT value FROM sandbox_store_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO sandbox_store_metadata VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
        elif row[0] != _SCHEMA_VERSION:
            raise KernelError("sandbox_store_version", "Sandbox Profile存储版本不受支持")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS sandbox_profiles ("
            "digest TEXT PRIMARY KEY, payload TEXT NOT NULL) STRICT"
        )

    def save(self, profile: ContainerSandboxProfile) -> None:
        checked = self._validate(profile)
        payload = checked.model_dump_json(warnings="error")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT payload FROM sandbox_profiles WHERE digest = ?", (checked.digest,)
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO sandbox_profiles VALUES (?, ?)", (checked.digest, payload)
                )
            elif row[0] != payload:
                raise KernelError("sandbox_profile_conflict", "Sandbox摘要已经绑定其他内容")
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def load(self, digest: str) -> ContainerSandboxProfile:
        row = self._db.execute(
            "SELECT payload FROM sandbox_profiles WHERE digest = ?", (digest,)
        ).fetchone()
        if row is None:
            raise KernelError("sandbox_profile_not_found", "Sandbox Profile不存在")
        try:
            return ContainerSandboxProfile.model_validate_json(row[0])
        except (ValidationError, ValueError, TypeError):
            raise KernelError("sandbox_store_corrupt", "Sandbox Profile存储记录损坏") from None

    @staticmethod
    def _validate(profile: ContainerSandboxProfile) -> ContainerSandboxProfile:
        try:
            return ContainerSandboxProfile.model_validate_json(
                profile.model_dump_json(warnings="error")
            )
        except (ValidationError, ValueError, TypeError):
            raise KernelError("sandbox_profile_invalid", "Sandbox Profile不符合契约") from None

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteSandboxProfileStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
