"""备份父链只以身份元数据句柄锚定，再以READ_CONTROL端口验权，不向Workspace扩大权限。"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.state_backup_files import (
    PrivateStateTree,
    _verify_windows_parent_chain,
)


@pytest.mark.parametrize("drift", [False, True])
def test_managed_parent_chain_uses_permission_handles_and_rechecks_original_identity(drift):
    opened, closed = [], []
    root = SimpleNamespace(
        _information=lambda handle: "different" if drift and handle == 101 else handle % 100,
        _object_identity=lambda info: info,
    )

    def open_directory(path, *, directory):
        assert directory
        opened.append(path)
        return 100 + len(opened)

    port = SimpleNamespace(
        root=root, open=open_directory, kernel=SimpleNamespace(CloseHandle=closed.append)
    )
    tree = object.__new__(PrivateStateTree)
    tree.path = Path("/state")
    tree._windows = tree._windows_keys = port
    with ExitStack() as resources:
        if drift:
            with pytest.raises(KernelError, match="产品备份文件"):
                _verify_windows_parent_chain(tree, ("session-auth", "key.v1"), [1, 2], resources)
        else:
            _verify_windows_parent_chain(tree, ("session-auth", "key.v1"), [1, 2], resources)
    assert opened[0] == tree.path
    assert closed == ([101] if drift else [102, 101])
    if not drift:
        assert opened[1] == tree.path / "session-auth"
