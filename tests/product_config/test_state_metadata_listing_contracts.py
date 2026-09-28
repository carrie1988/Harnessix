"""枚举只需身份与权限元数据；不能跳过锁、申请正文读取或改变原写锁共享。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from harnessix.product_config import state_backup_files as backup
from harnessix.session.maintenance_io import MaintenanceIOControl


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_listing_observes_every_file_with_native_metadata_only_on_windows(
    tmp_path, monkeypatch, platform
):
    paths = ("action-audit.db.runtime.lock", "owned.bin")
    for name in paths:
        (tmp_path / name).write_bytes(b"private")
    observed = []

    @contextmanager
    def open_file(relative, *, metadata_only=False):
        assert metadata_only is (platform == "nt")
        observed.append(relative)
        yield 1

    tree = SimpleNamespace(path=tmp_path, open_file=open_file, checkpoint=lambda: None)
    monkeypatch.setattr(backup, "os", SimpleNamespace(name=platform, listdir=os.listdir))
    assert backup._tree_files(tree, MaintenanceIOControl()) == paths
    assert observed == list(paths)


@pytest.mark.parametrize("arguments", [{"create": True}, {"writable": True}])
def test_metadata_mutation_is_rejected_before_root_or_file_effects(arguments):
    def forbidden():
        pytest.fail("元数据变更组合不能进入受管对象操作")

    tree = SimpleNamespace(checkpoint=forbidden)
    with pytest.raises(ValueError, match="元数据观察"):
        with backup._open_private_file(tree, "owned.bin", metadata_only=True, **arguments):
            pytest.fail("不应生成文件FD")
