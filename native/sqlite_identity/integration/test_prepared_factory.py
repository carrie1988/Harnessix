"""非 editable 安装件的正式工厂接线；不用测试 wrapper 补做身份验证。"""

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def seed(path, value="A"):
    database = sqlite3.connect(path)
    try:
        database.execute("CREATE TABLE records(value)")
        database.execute("INSERT INTO records VALUES (?)", (value,))
        database.commit()
    finally:
        database.close()
    return path


def ordinary_connection(folder):
    async def run():
        for read_only in (False, True):
            path = seed(folder / f"{read_only}.db")
            body = path.read_bytes()
            with factory.open_prepared_git_connection(path, read_only=read_only) as database:
                factory.require_prepared_git_connection(database, path)
                issued = factory._registered_prepared_connection(database)
                assert issued.native_identity is not None
                observe = factory._prepared_git_connection_observer(database, path)
                pure = factory._prepared_git_connection_lifecycle_observer(database)
                registration = factory._prepared_git_connection_registration_observer(database)
                with patch.object(factory, "check_prepared_identity", side_effect=AssertionError):
                    pure()
                    registration()

                async def child(observe=observe, database=database, path=path):
                    observe()
                    with pytest.raises(KernelError):
                        factory.require_prepared_git_connection(database, path)

                await asyncio.create_task(child())
                database.execute("BEGIN")
                assert database.execute("SELECT value FROM records").fetchone() == ("A",)
                factory.require_prepared_git_connection(database, path)
                if not read_only:
                    database.execute("INSERT INTO records VALUES ('rollback')")
                assert database.in_transaction
            assert path.read_bytes() == body
            with pytest.raises(KernelError):
                observe()
            with pytest.raises(sqlite3.ProgrammingError):
                database.execute("SELECT 1")

    asyncio.run(run())


def opened_other_file_then_restored(folder):
    """路径首末都是 A，但 SQLite 实际打开 B；必须在 yield 前拒绝。"""
    for read_only in (False, True):
        path = seed(folder / f"a-{read_only}.db")
        replacement = seed(folder / f"b-{read_only}.db", "B")
        saved = folder / f"saved-{read_only}.db"
        connect = sqlite3.connect
        opened = []

        def swap(
            *args,
            path=path,
            saved=saved,
            replacement=replacement,
            connect=connect,
            opened=opened,
            **kwargs,
        ):
            path.rename(saved)
            replacement.rename(path)
            try:
                database = connect(*args, **kwargs)
                opened.append(database)
                return database
            finally:
                path.rename(replacement)
                saved.rename(path)

        with patch.object(sqlite3, "connect", swap), pytest.raises(KernelError) as caught:
            with factory.open_prepared_git_connection(path, read_only=read_only):
                pytest.fail("实际打开错误文件的连接不得登记")
        assert caught.value.code == "git_prepared_link_host_invalid"
        assert not getattr(factory._owned, "connections", {})
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


def first_failure_and_cleanup(folder):
    for index, original in enumerate((asyncio.CancelledError(), TimeoutError("deadline"))):
        path = seed(folder / f"failure-{index}.db")
        with pytest.raises(type(original)) as caught:
            with factory.open_prepared_git_connection(path, read_only=False) as database:
                database.execute("BEGIN IMMEDIATE")
                database.execute("INSERT INTO records VALUES ('rollback')")
                raise original
        assert caught.value is original
        assert not factory._owned.connections
        with factory.open_prepared_git_connection(path, read_only=True) as fresh:
            assert fresh.execute("SELECT value FROM records").fetchall() == [("A",)]


CASES = (ordinary_connection, opened_other_file_then_restored, first_failure_and_cleanup)


@pytest.mark.parametrize("case", [case.__name__ for case in CASES])
def test_installed_factory(tmp_path, case):
    completed = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).resolve()), case, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=45,
    )
    (tmp_path / "run.log").write_text(completed.stdout + completed.stderr)
    assert completed.returncode == 0, completed.stdout + completed.stderr


if __name__ == "__main__":
    from harnessix.agent.errors import KernelError
    from harnessix.product_config import git_prepared_native_identity as identity

    # 新进程、无 worker 或 Store；由产品初始化入口装配，不直接调用桥的测试入口。
    identity.initialize_prepared_git_identity()
    from harnessix_sqlite_identity import _bridge

    from harnessix.product_config import git_prepared_link_connection as factory

    assert "site-packages" in Path(factory.__file__).parts
    assert "site-packages" in Path(identity.__file__).parts
    case = next(case for case in CASES if case.__name__ == sys.argv[1])
    case(Path(sys.argv[2]))
    counts = _bridge._resource_counts()
    assert counts["states_live"] == counts["leases_live"] == counts["userdata_live"] == 0
    (Path(sys.argv[2]) / "result.json").write_text(
        json.dumps({"status": "PASS", "resources": counts})
    )
