"""实际托管Session密钥：独立身份、持久重开、原文件边界与失败清理。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventDraft, ThreadCreated
from harnessix.product_config.session_key import open_product_session_binding
from harnessix.product_config.session_key_store import load_session_key
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.test_publication import protected


def state_root(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    return root


async def test_managed_key_reopens_actual_history_with_new_scope_and_original_bytes(tmp_path):
    root = state_root(tmp_path)
    tid, event = uuid4(), EventDraft(payload=ThreadCreated(workspace=tmp_path.as_posix()))
    with protected() as scope:
        async with open_product_session_binding(root, scope) as binding:
            store = SQLiteSessionStore(root / "sessions.db", publication=binding)
            await store.initialize()
            await store.append(tid, [event], expected_sequence=0)
            with sqlite3.connect(store.path) as db:
                original = db.execute("SELECT event_json FROM agent_events").fetchone()[0]
            key_copy = binding._key
    assert not any(key_copy)
    with protected() as scope:
        async with open_product_session_binding(root, scope) as binding:
            store = SQLiteSessionStore(root / "sessions.db", publication=binding)
            await store.initialize()
            assert (await store.get_thread(tid)).sequence == 1
            assert len(await store.events(tid)) == 1
            with sqlite3.connect(store.path) as db:
                assert db.execute("SELECT event_json FROM agent_events").fetchone()[0] == original


@pytest.mark.parametrize("name", ["sessions.db", "sessions.db-wal", "sessions.db-shm"])
def test_absent_key_never_bootstraps_over_previous_database_or_sidecars(tmp_path, name):
    root = state_root(tmp_path)
    path = root / name
    path.write_bytes(b"original-database-fixture")
    with pytest.raises(KernelError) as caught:
        load_session_key(root)
    assert caught.value.code == "publication_key_unavailable"
    assert path.read_bytes() == b"original-database-fixture"
    assert not (root / "session-auth/key.v1").exists()


def test_material_key_is_independent_stable_and_cleared(tmp_path):
    root = state_root(tmp_path)
    first = load_session_key(root)
    identity, saved = (first.store_id, first.key_id), bytes(first.key)
    second = load_session_key(root)
    assert identity == (second.store_id, second.key_id)
    assert bytes(second.key) == saved and len(saved) == 32
    assert "key=" not in repr(first)
    first.close()
    second.close()
    assert not any(first.key) and not any(second.key)


@pytest.mark.skipif(os.name != "posix", reason="POSIX私有目录观察")
@pytest.mark.parametrize("change", ["file_entry", "directory_entry", "timestamps"])
def test_key_root_allows_ordinary_metadata_change_without_changing_identity(
    tmp_path, monkeypatch, change
):
    from harnessix.product_config import session_key_posix

    root = state_root(tmp_path)
    first = load_session_key(root)
    expected = first.store_id, first.key_id
    first.close()
    identity = root.stat().st_dev, root.stat().st_ino
    original = session_key_posix._private_acl
    changed = False

    def alter_metadata(descriptor):
        nonlocal changed
        info = os.fstat(descriptor)
        if not changed and (info.st_dev, info.st_ino) == identity:
            changed = True
            if change == "file_entry":
                (root / "ordinary-state-file").touch()
            elif change == "directory_entry":
                (root / "ordinary-state-directory").mkdir()
            else:
                os.utime(root, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        original(descriptor)

    monkeypatch.setattr(session_key_posix, "_private_acl", alter_metadata)
    material = load_session_key(root)
    try:
        assert changed and (material.store_id, material.key_id) == expected
    finally:
        material.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX私有目录返回前安全复核")
@pytest.mark.parametrize("directory", ["root", "auth"])
def test_key_loading_rechecks_directory_permissions_after_read(tmp_path, directory):
    root = state_root(tmp_path)
    load_session_key(root).close()

    def change_permissions(stage):
        if stage == "key.before_return":
            (root if directory == "root" else root / "session-auth").chmod(0o755)

    with pytest.raises(KernelError) as failed:
        load_session_key(root, fault=change_permissions).close()
    assert failed.value.code == "publication_key_unavailable"


@pytest.mark.skipif(os.name != "posix", reason="POSIX目录FD与路径身份复核")
@pytest.mark.parametrize("directory", ["root", "auth"])
def test_key_loading_refuses_replaced_private_directory_after_read(tmp_path, directory):
    root = state_root(tmp_path)
    load_session_key(root).close()
    original = (root / "session-auth/key.v1").read_bytes()
    target = root if directory == "root" else root / "session-auth"
    moved = tmp_path / "moved-original"

    def replace_directory(stage):
        if stage == "key.before_return":
            target.rename(moved)
            target.mkdir(mode=0o700)

    with pytest.raises(KernelError) as failed:
        load_session_key(root, fault=replace_directory).close()
    assert failed.value.code == "publication_key_unavailable"
    key = moved / "session-auth/key.v1" if directory == "root" else moved / "key.v1"
    assert key.read_bytes() == original
    assert not (root / "session-auth/key.v1").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="实际Darwin返回前扩展ACL复核")
@pytest.mark.parametrize("directory", ["root", "auth"])
def test_key_loading_rechecks_native_directory_acl_after_read(tmp_path, directory):
    import subprocess

    root = state_root(tmp_path)
    load_session_key(root).close()
    target = root if directory == "root" else root / "session-auth"
    original = (root / "session-auth/key.v1").read_bytes()

    def grant_acl(stage):
        if stage == "key.before_return":
            subprocess.run(["chmod", "+a", "everyone allow read,execute", str(target)], check=True)

    try:
        with pytest.raises(KernelError) as failed:
            load_session_key(root, fault=grant_acl).close()
        assert failed.value.code == "publication_key_unavailable"
        assert target.stat().st_mode & 0o777 == 0o700
        assert (root / "session-auth/key.v1").read_bytes() == original
    finally:
        # 只移除自有临时Fixture的ACL，产品不会自动修复危险权限。
        subprocess.run(["chmod", "-N", str(target)], check=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX原生文件身份合同")
@pytest.mark.parametrize("case", ["permissions", "hardlink", "symlink", "directory", "corrupt"])
def test_existing_key_is_not_repaired_or_replaced(tmp_path, case):
    root = state_root(tmp_path)
    load_session_key(root).close()
    path = root / "session-auth/key.v1"
    original = path.read_bytes()
    if case == "permissions":
        path.chmod(0o644)
    elif case == "hardlink":
        os.link(path, root / "alias")
    elif case == "symlink":
        path.unlink()
        other = root / "other"
        other.write_bytes(original)
        path.symlink_to(other)
    elif case == "directory":
        path.unlink()
        path.mkdir()
    else:
        path.write_bytes(b"corrupt")
    with pytest.raises(KernelError) as caught:
        load_session_key(root)
    assert caught.value.code == "publication_key_unavailable"
    if case not in {"directory", "corrupt"}:
        assert path.read_bytes() == original
    if case == "permissions":
        assert path.stat().st_mode & 0o077 == 0o044


@pytest.mark.skipif(os.name != "posix", reason="POSIX目录FD/no-follow合同")
@pytest.mark.parametrize("case", ["root_permissions", "directory_permissions", "directory_link"])
def test_key_directory_is_private_and_never_followed(tmp_path, case):
    root = state_root(tmp_path)
    if case == "root_permissions":
        root.chmod(0o755)
    elif case == "directory_permissions":
        (root / "session-auth").mkdir(mode=0o755)
    else:
        other = tmp_path / "other"
        other.mkdir(mode=0o700)
        (root / "session-auth").symlink_to(other, target_is_directory=True)
    with pytest.raises(KernelError) as caught:
        load_session_key(root)
    assert caught.value.code == "publication_key_unavailable"
    assert not (root / "session-auth/key.v1").exists()


async def test_parent_cancel_waits_for_running_key_load_and_clears_returned_material(
    tmp_path, monkeypatch
):
    import threading

    root = state_root(tmp_path)
    entered, release = threading.Event(), threading.Event()
    material = load_session_key(root)

    def held_load(_root):
        entered.set()
        assert release.wait(5)
        return material

    monkeypatch.setattr("harnessix.product_config.session_key.load_session_key", held_load)

    async def use(scope):
        async with open_product_session_binding(root, scope):
            pytest.fail("已取消的调用不得发布Binding")

    with protected() as scope:
        task = asyncio.create_task(use(scope))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not any(material.key)


@pytest.mark.parametrize("point", ["key.after_pending", "key.after_publish", "key.before_return"])
def test_actual_process_exit_keeps_original_identity_recoverable(tmp_path, point):
    import hashlib
    import subprocess
    import sys

    root = state_root(tmp_path)
    program = """
from pathlib import Path
import hashlib,json,os,sys
from harnessix.product_config.session_key_store import load_session_key
root=Path(sys.argv[1]);point=sys.argv[2]
def fault(value):
    if value==point:os._exit(70)
load_session_key(root,fault=fault)
"""
    result = subprocess.run([sys.executable, "-c", program, str(root), point], check=False)
    assert result.returncode == 70
    candidate = (
        root / "session-auth" / ("key.v1.pending" if point == "key.after_pending" else "key.v1")
    )
    original = hashlib.sha256(candidate.read_bytes()).hexdigest()
    material = load_session_key(root)
    try:
        assert hashlib.sha256((root / "session-auth/key.v1").read_bytes()).hexdigest() == original
        assert not (root / "session-auth/key.v1.pending").exists()
        assert len(material.key) == 32
    finally:
        material.close()


@pytest.mark.parametrize("case", ["empty", "huge", "wrong_version", "trailing", "wrong_envelope"])
def test_invalid_persistent_format_does_not_generate_another_key(tmp_path, case):
    root = state_root(tmp_path)
    load_session_key(root).close()
    path = root / "session-auth/key.v1"
    original = path.read_bytes()
    value = {
        "empty": b"",
        "huge": b"x" * 8193,
        "wrong_version": original[:4] + b"\xff" + original[5:],
        "trailing": original + b"x",
        "wrong_envelope": b"not-a-key" + original,
    }[case]
    path.write_bytes(value)
    with pytest.raises(KernelError) as caught:
        load_session_key(root)
    assert caught.value.code == "publication_key_unavailable"
    assert path.read_bytes() == value


@pytest.mark.skipif(os.name != "posix", reason="POSIX原生互斥锁合同")
def test_concurrent_bootstrap_reports_busy_without_alternate_key(tmp_path):
    from harnessix.file_lock import acquire_exclusive_file_lock

    root = state_root(tmp_path)
    material = load_session_key(root)
    path = root / "session-auth/key.v1"
    original = path.read_bytes()
    descriptor = os.open(root / "session-auth/.lock", os.O_RDWR)
    try:
        acquire_exclusive_file_lock(descriptor)
        with pytest.raises(KernelError) as caught:
            load_session_key(root)
        assert caught.value.code == "publication_key_busy"
        assert path.read_bytes() == original
    finally:
        os.close(descriptor)
        material.close()


async def test_missing_committed_key_refuses_reopen_without_replacing_original_history(tmp_path):
    root = state_root(tmp_path)
    tid = uuid4()
    with protected() as scope:
        async with open_product_session_binding(root, scope) as binding:
            store = SQLiteSessionStore(root / "sessions.db", publication=binding)
            await store.initialize()
            await store.append(
                tid,
                [EventDraft(payload=ThreadCreated(workspace=tmp_path.as_posix()))],
                expected_sequence=0,
            )
    with sqlite3.connect(store.path) as db:
        original = db.execute("SELECT event_json FROM agent_events").fetchall()
    (root / "session-auth/key.v1").unlink()
    with pytest.raises(KernelError) as caught:
        load_session_key(root)
    assert caught.value.code == "publication_key_unavailable"
    assert not (root / "session-auth/key.v1").exists()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT event_json FROM agent_events").fetchall() == original


async def test_key_timeout_settles_single_worker_and_clears_material(tmp_path, monkeypatch):
    import threading

    from harnessix.product_config import session_key

    root = state_root(tmp_path)
    material = load_session_key(root)
    entered, release = threading.Event(), threading.Event()
    tasks = []

    def held_load(_root):
        tasks.append(1)
        entered.set()
        assert release.wait(5)
        return material

    monkeypatch.setattr(session_key, "load_session_key", held_load)
    monkeypatch.setattr(session_key, "KEY_LOAD_TIMEOUT_SECONDS", 0.01)
    original = session_key._load_owned
    settled = asyncio.Event()

    async def tracked_load(path):
        try:
            return await original(path)
        except asyncio.CancelledError:
            settled.set()
            raise

    monkeypatch.setattr(session_key, "_load_owned", tracked_load)

    async def use(scope):
        async with open_product_session_binding(root, scope):
            pytest.fail("超时调用不得发布Binding")

    with protected() as scope:
        task = asyncio.create_task(use(scope))
        assert await asyncio.to_thread(entered.wait, 5)
        # 定时器已触发后允许唯一线程结算；不以超时冒充线程被强杀。
        await asyncio.sleep(0.03)
        release.set()
        with pytest.raises(KernelError) as caught:
            await task
        assert caught.value.code == "publication_key_timeout"
    assert settled.is_set() and tasks == [1] and not any(material.key)


async def test_closed_scope_never_publishes_binding_and_clears_loaded_key(tmp_path, monkeypatch):
    root = state_root(tmp_path)
    material = load_session_key(root)
    monkeypatch.setattr("harnessix.product_config.session_key.load_session_key", lambda _: material)
    scope = protected()
    with scope:
        pass
    with pytest.raises(KernelError) as caught:
        async with open_product_session_binding(root, scope):
            pytest.fail("失效Scope不得签发")
    assert caught.value.code == "publication_scope_unavailable"
    assert not any(material.key)


@pytest.mark.skipif(sys.platform != "darwin", reason="实际Darwin扩展ACL")
@pytest.mark.parametrize("target", ["root", "directory", "key"])
def test_native_macos_extended_acl_does_not_bypass_private_mode_bits(tmp_path, target):
    import subprocess

    root = state_root(tmp_path)
    load_session_key(root).close()
    key = root / "session-auth/key.v1"
    original = key.read_bytes()
    path = {"root": root, "directory": root / "session-auth", "key": key}[target]
    rights = "read" if target == "key" else "read,execute"
    subprocess.run(["chmod", "+a", "everyone allow " + rights, str(path)], check=True)
    try:
        with pytest.raises(KernelError) as caught:
            load_session_key(root)
        assert caught.value.code == "publication_key_unavailable"
        assert key.read_bytes() == original
    finally:
        # 测试只清理自己创建的临时ACL，不修复产品既有文件。
        subprocess.run(["chmod", "-N", str(path)], check=True)
