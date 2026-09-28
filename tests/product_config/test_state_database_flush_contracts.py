"""SQLite完整复制后再打开可Flush的原私有Handle，不持有数据库连接或升级读端口权限。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.product_config import state_backup as backup
from harnessix.session.maintenance_io import MaintenanceIOControl


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_database_flush_uses_writable_handle_on_windows_after_connections_close(
    monkeypatch, platform
):
    calls, databases = [], []

    @contextmanager
    def original_file(path):
        assert path == "sessions.db"
        yield 1

    @contextmanager
    def target_file(path, **arguments):
        calls.append(arguments)
        if not arguments.get("create"):
            assert all(database.closed for database in databases)
            assert arguments.get("writable", False) is (platform == "nt")
        yield 2

    class Database:
        closed = False

        def execute(self, sql):
            return SimpleNamespace(fetchone=lambda: (4096,))

        def backup(self, writer, *, pages, progress, sleep):
            assert pages == 128
            progress(0, 0, 1)

        def commit(self):
            pass

        def close(self):
            self.closed = True

    def connect(*args, **kwargs):
        database = Database()
        databases.append(database)
        return database

    source = SimpleNamespace(path=Path("/source"), open_file=original_file)
    target = SimpleNamespace(path=Path("/target"), open_file=target_file)
    flushed = []
    monkeypatch.setattr(backup, "os", SimpleNamespace(name=platform, fsync=flushed.append))
    monkeypatch.setattr(backup.sqlite3, "connect", connect)
    backup._copy_database(source, target, "sessions.db", MaintenanceIOControl())
    assert calls[0] == {"create": True} and len(calls) == 2
    assert flushed == [2, 2] and len(databases) == 2
