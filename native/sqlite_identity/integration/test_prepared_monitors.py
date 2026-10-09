"""安装件四库实际 monitor 的原生身份；每个 case 独占新进程显式 startup。"""

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@contextmanager
def monitor_databases(folder, *, wal=True, distinct_versions=False):
    folder.mkdir(parents=True, exist_ok=True)
    paths = tuple(folder / f"{name}.db" for name in ("session", "audit", "plans", "core"))
    with ExitStack() as stack:
        writers = tuple(
            stack.enter_context(closing(sqlite3.connect(path, isolation_level=None)))
            for path in paths
        )
        for writer in writers:
            if wal:
                assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
            writer.execute("CREATE TABLE records(value)")
            writer.execute("INSERT INTO records VALUES ('A')")
        router = SimpleNamespace(
            _audit=SimpleNamespace(_path=paths[1], _db=writers[1]),
            _plans=SimpleNamespace(_path=paths[2], _db=writers[2]),
        )
        core = SimpleNamespace(store=SimpleNamespace(_path=paths[3], _db=writers[3]))
        artifacts = SimpleNamespace(session=SimpleNamespace(path=paths[0]))
        opened = []
        readonly = factory.readonly_database

        def open_reader(path):
            database = readonly(path)
            opened.append(database)
            index = paths.index(path)
            if distinct_versions:
                database.execute("PRAGMA data_version").fetchone()
                for _ in range(index):
                    writers[index].execute("INSERT INTO records VALUES ('WAL')")
                    database.execute("PRAGMA data_version").fetchone()
            return database

        stack.enter_context(patch.object(factory, "readonly_database", open_reader))
        yield SimpleNamespace(
            args=(router, core, artifacts), paths=paths, writers=writers, opened=opened
        )
    assert_resources_released()


def assert_resources_released():
    counts = _bridge._resource_counts()
    assert counts["states_live"] == counts["leases_live"] == counts["userdata_live"] == 0


def assert_monitors_closed(databases, unchanged=None):
    assert not getattr(factory._owned, "connections", {})
    for database in databases.opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            database.execute("SELECT 1")
    counts = _bridge._resource_counts()
    assert counts["leases_live"] == counts["userdata_live"] == 0
    if unchanged is not None:
        with changed():
            unchanged()


@contextmanager
def changed():
    with pytest.raises(KernelError) as caught:
        yield
    assert caught.value.code == "git_prepared_link_changed"


def four_readonly_wal_monitors_and_child(folder):
    async def run():
        with monitor_databases(folder, distinct_versions=True) as databases:
            with observation.observe_prepared_state(*databases.args) as unchanged:
                assert len(databases.opened) == len(set(databases.opened)) == 4
                assert _bridge._resource_counts()["leases_live"] == 4
                assert len({observation._version(db) for db in databases.opened}) == 4
                for index, (db, path) in enumerate(
                    zip(databases.opened, databases.paths, strict=True)
                ):
                    issued = factory._registered_prepared_connection(db)
                    assert issued.path == path
                    assert issued.task is asyncio.current_task()
                    assert issued.native_identity is not None
                    assert issued.native_identity.check() is True
                    assert db.execute("SELECT value FROM records").fetchall() == (
                        [("A",)] + [("WAL",)] * index
                    )
                    assert db.execute("PRAGMA query_only").fetchone() == (1,)
                    for mode in ("ON", "OFF"):
                        db.execute(f"PRAGMA query_only={mode}")
                        with pytest.raises(sqlite3.OperationalError, match="readonly"):
                            db.execute("INSERT INTO records VALUES ('forbidden')")
                    assert db.total_changes == 0
                files = set(folder.iterdir())
                before = {
                    path: path.read_bytes() for path in files if not path.name.endswith("-shm")
                }

                async def child():
                    unchanged()
                    for db, path in zip(databases.opened, databases.paths, strict=True):
                        for operation in (
                            factory.require_prepared_git_connection,
                            factory._prepared_git_connection_observer,
                        ):
                            with pytest.raises(KernelError) as caught:
                                operation(db, path)
                            assert caught.value.code == "git_prepared_link_host_invalid"
                        with pytest.raises(KernelError) as caught:
                            with git_prefix_sql_window(db, checkpoint=unchanged):
                                pytest.fail("child must not acquire a SQL window")
                        assert caught.value.code == "git_prepared_link_host_invalid"

                await asyncio.create_task(child())
                unchanged()
            assert_monitors_closed(databases, unchanged)
            assert set(folder.iterdir()) == files
            assert {path: path.read_bytes() for path in before} == before

    asyncio.run(run())


def opened_b_then_restored_a_rejects_before_yield(folder):
    for index in range(4):
        with monitor_databases(folder / str(index), wal=False) as databases:
            path = databases.paths[index]
            replacement, saved = path.with_suffix(".other"), path.with_suffix(".saved")
            with closing(sqlite3.connect(replacement)) as other:
                other.execute("CREATE TABLE records(value)")
                other.execute("INSERT INTO records VALUES ('B')")
                other.commit()
            original_identity = observation._identity(path)
            readonly = factory.readonly_database

            def swap(
                candidate,
                path=path,
                replacement=replacement,
                saved=saved,
                readonly=readonly,
            ):
                if candidate != path:
                    return readonly(candidate)
                path.rename(saved)
                replacement.rename(path)
                try:
                    database = readonly(candidate)
                    assert database.execute("SELECT value FROM records").fetchone() == ("B",)
                    return database
                finally:
                    path.rename(replacement)
                    saved.rename(path)

            with patch.object(factory, "readonly_database", swap), changed():
                with observation.observe_prepared_state(*databases.args):
                    pytest.fail("actual B handle must be rejected before monitor yield")
            assert observation._identity(path) == original_identity
            assert len(databases.opened) == index + 1
            assert_monitors_closed(databases)


def full_rechecks_every_actual_native_token(folder):
    for index in range(4):
        with monitor_databases(folder / str(index)) as databases:
            with changed(), observation.observe_prepared_state(*databases.args) as unchanged:
                token = factory._registered_prepared_connection(
                    databases.opened[index]
                ).native_identity
                token.release()
                unchanged()
            assert_monitors_closed(databases, unchanged)


def each_database_version_and_writer_changes(folder):
    for index in range(4):
        with monitor_databases(folder / f"version-{index}") as databases:
            with changed(), observation.observe_prepared_state(*databases.args) as unchanged:
                with closing(sqlite3.connect(databases.paths[index])) as other:
                    other.execute("INSERT INTO records VALUES ('external')")
                    other.commit()
                unchanged()
            assert_monitors_closed(databases, unchanged)
    for index in (1, 2, 3):
        with monitor_databases(folder / f"writer-{index}") as databases:
            with changed(), observation.observe_prepared_state(*databases.args) as unchanged:
                before = tuple(observation._version(db) for db in databases.opened)
                writer = databases.writers[index]
                writer.execute("BEGIN")
                writer.execute("INSERT INTO records VALUES ('rollback')")
                writer.rollback()
                assert tuple(observation._version(db) for db in databases.opened) == before
                unchanged()
            assert_monitors_closed(databases, unchanged)


def first_failure_and_cleanup(folder):
    for index, original in enumerate(
        (
            asyncio.CancelledError(),
            TimeoutError("deadline"),
            RuntimeError("external callback"),
            KernelError("foreign_callback", "first failure"),
            KernelError("git_prepared_link_host_invalid", "body is not a source observer"),
        )
    ):
        with monitor_databases(folder / str(index)) as databases:
            with pytest.raises(type(original)) as caught:
                with observation.observe_prepared_state(*databases.args) as unchanged:
                    assert _bridge._resource_counts()["leases_live"] == 4
                    raise original
            assert caught.value is original
            assert_monitors_closed(databases, unchanged)


def retained_scope_skips_only_exit_recheck(folder):
    with monitor_databases(folder) as databases:
        with observation.observe_prepared_state(*databases.args, check_on_exit=False) as unchanged:
            databases.writers[1].execute("INSERT INTO records VALUES ('committed')")
            with changed():
                unchanged()
        assert_monitors_closed(databases, unchanged)


CASES = (
    four_readonly_wal_monitors_and_child,
    opened_b_then_restored_a_rejects_before_yield,
    full_rechecks_every_actual_native_token,
    each_database_version_and_writer_changes,
    first_failure_and_cleanup,
    retained_scope_skips_only_exit_recheck,
)


@pytest.mark.parametrize("case", [case.__name__ for case in CASES])
def test_installed_monitors(tmp_path, case):
    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), case, str(tmp_path)],
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

    assert sys.version_info[:2] == (3, 12)
    assert sqlite3.sqlite_version == "3.45.3"
    identity.initialize_prepared_git_identity()
    from harnessix_sqlite_identity import _bridge

    from harnessix.product_config import git_prepared_link_connection as factory
    from harnessix.product_config import git_prepared_link_observation as observation
    from harnessix.product_config.git_prefix_sql import git_prefix_sql_window

    for module in (factory, observation, identity):
        assert "site-packages" in Path(module.__file__).parts
    case = next(case for case in CASES if case.__name__ == sys.argv[1])
    folder = Path(sys.argv[2])
    case(folder)
    assert_resources_released()
    (folder / "result.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "python": sys.version,
                "sqlite": sqlite3.sqlite_version,
                "observation": observation.__file__,
                "observation_sha256": hashlib.sha256(
                    Path(observation.__file__).read_bytes()
                ).hexdigest(),
                "resources": _bridge._resource_counts(),
            },
            indent=2,
        )
    )
