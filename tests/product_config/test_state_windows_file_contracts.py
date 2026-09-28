"""Windows备份叶对象必须以同一原生修订语义验真，并回收独立元数据句柄。"""

from __future__ import annotations

import ctypes
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import state_backup_files as backup
from harnessix.product_config.session_key_windows_files import WindowsKeyFiles
from harnessix.workspace.windows import WindowsWorkspaceRoot


def information():
    return SimpleNamespace(
        volume_serial=1,
        file_index_high=2,
        file_index_low=3,
        attributes=32,
        links=1,
        size_high=0,
        size_low=1024,
        write_time=SimpleNamespace(high=4, low=5),
        creation_time=SimpleNamespace(high=6, low=7),
    )


@pytest.mark.parametrize(
    "changed",
    [
        None,
        "volume_serial",
        "file_index_high",
        "file_index_low",
        "attributes",
        "links",
        "size_high",
        "size_low",
        "write_time.high",
        "write_time.low",
        "creation_time.high",
        "creation_time.low",
    ],
)
@pytest.mark.parametrize("drift_at", ["path", "original-after"])
def test_native_file_revision_binds_path_and_original_handle_and_always_closes(changed, drift_at):
    original = information()
    different = deepcopy(original)
    if changed:
        target = different
        parts = changed.split(".")
        for part in parts[:-1]:
            target = getattr(target, part)
        setattr(target, parts[-1], getattr(target, parts[-1]) + 1)
    opened, closed, verified = [], [], []
    observations = iter(
        [
            original,
            different if drift_at == "path" else original,
            different if drift_at == "original-after" else original,
        ]
    )

    def open_metadata(path, *, metadata_only):
        assert metadata_only is True
        opened.append(path)
        return 12

    native = SimpleNamespace(
        _information=lambda handle: next(observations),
        _revision_identity=WindowsWorkspaceRoot._revision_identity,
    )
    port = SimpleNamespace(
        root=native,
        open=open_metadata,
        kernel=SimpleNamespace(CloseHandle=closed.append),
        security=SimpleNamespace(verify=verified.append),
    )
    tree = object.__new__(backup.PrivateStateTree)
    tree.path = Path("/private")
    tree._windows = tree._windows_keys = port
    if changed:
        with pytest.raises(KernelError, match="产品备份文件"):
            backup._verify_windows_file(tree, "session-auth/key.v1", 11)
    else:
        backup._verify_windows_file(tree, "session-auth/key.v1", 11)
    assert opened == [tree.path / "session-auth/key.v1"]
    assert closed == [12]
    assert verified == ([] if changed else [11])


def test_native_file_permission_failure_still_closes_metadata_handle():
    native = SimpleNamespace(
        _information=lambda handle: information(),
        _revision_identity=WindowsWorkspaceRoot._revision_identity,
    )
    closed = []

    def refuse(handle):
        raise KernelError("publication_key_unavailable", "Session密钥不可用")

    port = SimpleNamespace(
        root=native,
        open=lambda *args, **kwargs: 12,
        kernel=SimpleNamespace(CloseHandle=closed.append),
        security=SimpleNamespace(verify=refuse),
    )
    tree = object.__new__(backup.PrivateStateTree)
    tree.path = Path("/private")
    tree._windows = tree._windows_keys = port
    with pytest.raises(KernelError, match="Session密钥"):
        backup._verify_windows_file(tree, "session-auth/key.v1", 11)
    assert closed == [12]


@pytest.mark.parametrize(
    "arguments,access,share",
    [
        ({}, 0x80020000, 1),
        ({"metadata_only": True}, 0x20080, 3),
        ({"create": 1, "writable": True, "exclusive": True}, 0xC0020000, 0),
    ],
)
def test_metadata_observation_does_not_weaken_key_data_or_exclusive_write_sharing(
    arguments, access, share
):
    calls, verified = [], []

    def create(*values):
        calls.append(values)
        return 11

    files = object.__new__(WindowsKeyFiles)
    files._read_share = 1
    files.root = SimpleNamespace(_information=lambda handle: information())
    files.kernel = SimpleNamespace(CreateFileW=create, CloseHandle=lambda handle: None)
    files.security = SimpleNamespace(attributes=ctypes.c_int(), verify=verified.append)
    assert files.open(Path("/private/file.bin"), **arguments) == 11
    assert (calls[0][1], calls[0][2]) == (access, share)
    assert verified == [11]


@pytest.mark.parametrize(
    "arguments", [{"create": 1}, {"writable": True}, {"exclusive": True}, {"directory": True}]
)
def test_metadata_observation_rejects_mutating_or_directory_modes(arguments):
    files = object.__new__(WindowsKeyFiles)
    with pytest.raises(ValueError, match="元数据句柄"):
        files.open(Path("/private/file.bin"), metadata_only=True, **arguments)
