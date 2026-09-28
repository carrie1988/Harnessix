"""Windows原生私有备份文件端口；不作为默认产品完整Windows备份验收。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from contextlib import closing

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.state_backup_files import (
    PrivateStateTree,
    create_private_tree,
    publish_tree,
    read_small,
    write_new,
)
from harnessix.session.maintenance_io import MaintenanceIOControl

pytestmark = pytest.mark.skipif(os.name != "nt", reason="仅在原生Windows验证Handle与DACL")


def test_running_state_directory_and_sqlite_children_have_private_inheritance(tmp_path):
    from harnessix.product_config.server import _private_root

    root = _private_root(tmp_path / "state")
    with closing(sqlite3.connect(root / "database.db")) as database:
        database.execute("CREATE TABLE private_state (value TEXT)")
        database.commit()
        database.execute("INSERT INTO private_state VALUES ('private fixture')")
        # 真实SQLite保留写锁时，原Handle读取必须兼容其读写共享，不允许DELETE共享。
        assert (root / "database.db-journal").exists()
        with PrivateStateTree(root) as tree:
            assert tree.files(MaintenanceIOControl()) == ("database.db", "database.db-journal")
            assert read_small(tree, "database.db", 65536).startswith(b"SQLite format 3")
        database.rollback()


def test_ordinary_mkdir_acl_is_rejected_without_repair(tmp_path):
    from harnessix.product_config.server import _private_root

    root = tmp_path / "legacy"
    root.mkdir(mode=0o700)
    before = subprocess.run(["icacls", str(root)], capture_output=True, check=True).stdout
    with pytest.raises(KernelError):
        _private_root(root)
    after = subprocess.run(["icacls", str(root)], capture_output=True, check=True).stdout
    assert after == before


@pytest.mark.parametrize("target", ["root", "file"])
def test_public_acl_is_rejected_without_repairing_existing_state(tmp_path, target):
    from harnessix.product_config.server import _private_root

    root = _private_root(tmp_path / "state")
    path = root / "fixture.bin"
    path.write_bytes(b"private fixture")
    changed = root if target == "root" else path
    subprocess.run(
        ["icacls", str(changed), "/grant", "*S-1-1-0:R"], capture_output=True, check=True
    )
    before = subprocess.run(["icacls", str(changed)], capture_output=True, check=True).stdout
    with pytest.raises(KernelError):
        with PrivateStateTree(root) as tree:
            read_small(tree, "fixture.bin", 64)
    after = subprocess.run(["icacls", str(changed)], capture_output=True, check=True).stdout
    assert after == before and path.read_bytes() == b"private fixture"


@pytest.mark.parametrize("directory", ["session-auth", "SESSION-AUTH"])
def test_key_subtree_never_accepts_ordinary_state_acl(tmp_path, directory):
    root = tmp_path / "private"
    create_private_tree(root)
    from harnessix.workspace.windows_private_directory import private_state_directory

    # 即便仅用户/SYSTEM可访问，Key目录的可继承形态也不能代替原Key精确合同。
    private_state_directory(root / "session-auth")
    with PrivateStateTree(root) as tree:
        with pytest.raises(KernelError):
            write_new(tree, f"{directory}/key.v1", b"not-a-key")
    assert not (root / "session-auth/key.v1").exists()


def test_private_large_file_uses_native_handle_not_key_size_limit(tmp_path, monkeypatch):
    from tests.product_config.windows_backup_diagnostics import install_backup_diagnostics

    install_backup_diagnostics(monkeypatch)
    root = tmp_path / "private"
    create_private_tree(root)
    body = b"bounded-contents\n" * 4096
    with PrivateStateTree(root) as tree:
        write_new(tree, "nested/contents.bin", body)
        assert read_small(tree, "nested/contents.bin", len(body)) == body
        assert tree.files(MaintenanceIOControl()) == ("nested/contents.bin",)
    published = tmp_path / "published"
    publish_tree(root, published)
    assert not root.exists() and published.exists()


def test_directory_publication_never_replaces_existing_target(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    create_private_tree(first)
    create_private_tree(second)
    with PrivateStateTree(second) as tree:
        write_new(tree, "preserved.bin", b"keep")
    with pytest.raises(KernelError):
        publish_tree(first, second)
    assert (second / "preserved.bin").read_bytes() == b"keep"
    assert first.exists()


def test_private_reader_rejects_hardlink(tmp_path):
    root = tmp_path / "private"
    create_private_tree(root)
    with PrivateStateTree(root) as tree:
        write_new(tree, "original.bin", b"keep")
    # 先构造既有多链接事实；避免活跃树拒绝DELETE共享干扰坏事实的准备。
    os.link(root / "original.bin", root / "alias.bin")
    with PrivateStateTree(root) as tree:
        with pytest.raises(KernelError):
            read_small(tree, "original.bin", 16)


@pytest.mark.parametrize("fault", ["none", "body", "verification"])
def test_native_fd_and_file_handles_close_before_tree_exit(tmp_path, monkeypatch, fault):
    from harnessix.product_config import state_backup_files as backup

    root = tmp_path / "private"
    create_private_tree(root)
    with PrivateStateTree(root) as tree:
        handles = []
        original = tree._windows.open

        def observe_open(path, **arguments):
            handle = original(path, **arguments)
            if not arguments.get("directory", False):
                handles.append(handle)
            return handle

        def refuse(*args):
            raise backup.file_error()

        monkeypatch.setattr(tree._windows, "open", observe_open)
        if fault == "verification":
            monkeypatch.setattr(backup, "_verify_windows_file", refuse)

        def write():
            with tree.open_file("original.bin", create=True) as descriptor:
                os.write(descriptor, b"keep")
                os.fsync(descriptor)
                if fault == "body":
                    raise backup.file_error()
                return descriptor

        if fault == "none":
            descriptor = write()
            with pytest.raises(OSError):
                os.fstat(descriptor)
        else:
            with pytest.raises(KernelError, match="产品备份文件"):
                write()
        assert handles
        # GetFileInformationByHandle只观察原句柄；不能靠重开Path掩盖仍存活的原Handle。
        for handle in handles:
            with pytest.raises(OSError) as closed:
                tree._windows.root._information(handle)
            assert closed.value.args[0] == 6  # ERROR_INVALID_HANDLE


def test_private_reader_rejects_junction_before_reading(tmp_path):
    root, outside = tmp_path / "private", tmp_path / "outside"
    create_private_tree(root)
    create_private_tree(outside)
    with PrivateStateTree(outside) as tree:
        write_new(tree, "private.bin", b"keep")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "linked"), str(outside)],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    try:
        with PrivateStateTree(root) as tree:
            with pytest.raises(KernelError):
                read_small(tree, "linked/private.bin", 16)
    finally:
        os.rmdir(root / "linked")
