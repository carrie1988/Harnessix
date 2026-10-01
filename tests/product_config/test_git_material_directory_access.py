"""目录链的私有ACL读取必须持有READ_CONTROL；模拟权限不是原生NTFS验收。"""

from __future__ import annotations

import os
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from harnessix.delivery import git_material_native_windows as native
from harnessix.delivery.git_material_input_contracts import GitMaterialInputError
from harnessix.delivery.store import _prepare_directory


class _DirectoryAPI:
    """仅模拟句柄授予掩码和ACL读取前置，不模拟真实Windows私有性。"""

    def __init__(self):
        self.opens = []
        self.grants = {}
        self.checked = []
        self.closed = []
        self.kernel = SimpleNamespace(
            GetDriveTypeW=lambda anchor: 3,
            GetVolumeInformationByHandleW=self._volume,
        )

    def _volume(self, handle, name, count, serial, length, flags, filesystem, size):
        filesystem.value = "NTFS"
        return 1

    def open(self, path, resources, *, directory, private=False):
        assert directory
        handle = len(self.opens) + 1
        self.opens.append((path, handle, private))
        self.grants[handle] = native._held_access(private=private)
        resources.callback(self.closed.append, handle)
        if private:
            self.private(handle, directory=True)
        return handle

    def private(self, handle, *, directory):
        assert directory
        if not self.grants[handle] & 0x20000:
            raise GitMaterialInputError("git_material_private_invalid")
        self.checked.append(handle)


@pytest.mark.parametrize("cached", (False, True))
def test_private_directory_chain_requests_acl_read_access_without_releasing_guard(tmp_path, cached):
    api = _DirectoryAPI()
    cache = {}
    with ExitStack() as resources:
        old = native._chain(api, tmp_path, resources, cache) if cached else None
        handle = native._chain(api, tmp_path, resources, cache, private=True)
        assert api.grants[handle] == 0x20081
        assert api.checked == [handle]
        assert not api.closed
        if cached:
            assert old != handle and api.grants[old] == 0x81
            assert cache[tmp_path] == old
        assert all(mask in {0x81, 0x20081} for mask in api.grants.values())
        assert all(
            mask == 0x81 for path, h, _ in api.opens if path != tmp_path for mask in [api.grants[h]]
        )
    assert sorted(api.closed) == sorted(api.grants)


def test_nonprivate_directory_chain_keeps_original_minimal_access(tmp_path):
    api = _DirectoryAPI()
    with ExitStack() as resources:
        cache = {}
        handle = native._chain(api, tmp_path, resources, cache)
        assert api.grants[handle] == 0x81
        assert not api.checked
        assert all(mask == 0x81 for mask in api.grants.values())


def test_private_directory_rechecks_acl_on_each_observation(tmp_path):
    api = _DirectoryAPI()
    with ExitStack() as resources:
        cache = {}
        first = native._chain(api, tmp_path, resources, cache, private=True)
        second = native._chain(api, tmp_path, resources, cache, private=True)
        assert first != second
        assert api.checked == [first, second]
        assert not api.closed


@pytest.mark.skipif(os.name != "nt", reason="需要真实Windows本地NTFS及DACL")
@pytest.mark.parametrize("cached", (False, True))
def test_windows_actual_private_directory_chain_reads_acl_with_original_cached_guard(
    tmp_path, cached
):
    root = tmp_path / "material-private"
    _prepare_directory(root)
    api = native._Windows()
    with ExitStack() as resources:
        cache = {}
        if cached:
            native._chain(api, root, resources, cache)
        handle = native._chain(api, root, resources, cache, private=True)
        api.private(handle, directory=True)
        native._chain(api, root, resources, cache, private=True)
    assert root.is_dir()
