"""Windows原生私有备份文件端口；不作为默认产品完整Windows备份验收。"""

from __future__ import annotations

import os
import subprocess

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


def test_private_large_file_uses_native_handle_not_key_size_limit(tmp_path):
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
        os.link(root / "original.bin", root / "alias.bin")
        with pytest.raises(KernelError):
            read_small(tree, "original.bin", 16)


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
