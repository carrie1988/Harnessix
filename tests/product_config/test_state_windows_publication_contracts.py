"""私有状态Rename保留父链与源Handle，禁止覆盖和危险对象，任意失败均关闭。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import state_backup_windows as port


@pytest.mark.parametrize(
    "scenario",
    ["file", "directory", "rename-fails", "missing", "reparse", "hardlink", "permission-fails"],
)
def test_publication_keeps_original_parent_source_and_nonreplace_contract(monkeypatch, scenario):
    calls, closed = [], []
    root = SimpleNamespace(
        path=Path("/private"),
        _kernel32=SimpleNamespace(CloseHandle=lambda value: closed.append("source")),
        close=lambda: closed.append("root"),
        _open_chain=lambda *args, **kwargs: ([1, 2], "bound"),
        _close_all=lambda handles: closed.append("parents"),
        _information=lambda handle: SimpleNamespace(
            attributes=0x10
            if scenario == "directory"
            else 0x400
            if scenario == "reparse"
            else 0x20,
            links=2 if scenario == "hardlink" else 1,
        ),
    )

    def verify(handle):
        calls.append("verify")
        if scenario == "permission-fails":
            raise KernelError("product_state_invalid", "产品状态权限无效")

    security = SimpleNamespace(
        verify=verify, verify_root=lambda handle: calls.append("verify-root")
    )

    def files(bound, *, security_factory):
        assert bound is root and security_factory is port.PrivateStateSecurity
        return SimpleNamespace(security=security, close=lambda: closed.append("security"))

    def source(path, *, delete):
        assert path == Path("/private/source") and delete is True
        return None if scenario == "missing" else 3

    def rename(handle, name, *, replace):
        assert handle == 3 and name == "target" and replace is False
        assert not closed
        calls.append("rename")
        if scenario == "rename-fails":
            raise OSError(32, "native shared handle failure")

    operations = SimpleNamespace(
        open_existing=source,
        rename=rename,
        require_local_ntfs=lambda parent, anchor: calls.append("ntfs"),
    )
    monkeypatch.setattr(port, "WindowsWorkspaceRoot", lambda parent: root)
    monkeypatch.setattr(port, "WindowsKeyFiles", files)
    monkeypatch.setattr(port, "WindowsFileOperations", lambda kernel: operations)
    if scenario in {"file", "directory"}:
        port.publish_private_object(Path("/private/source"), Path("/private/target"))
        assert calls == ["ntfs", "verify-root" if scenario == "directory" else "verify", "rename"]
    else:
        with pytest.raises(KernelError):
            port.publish_private_object(Path("/private/source"), Path("/private/target"))
        assert ("rename" in calls) is (scenario == "rename-fails")
    assert closed == (
        ["security", "parents", "root"]
        if scenario == "missing"
        else ["source", "security", "parents", "root"]
    )


@pytest.mark.parametrize(
    "source,target",
    [
        ("source", "/private/target"),
        ("/private/source", "target"),
        ("/other/source", "/private/target"),
    ],
)
def test_publication_rejects_unbound_or_other_parent_before_open(monkeypatch, source, target):
    def forbidden(*args):
        pytest.fail("不合法范围不能打开原生句柄")

    monkeypatch.setattr(port, "WindowsWorkspaceRoot", forbidden)
    with pytest.raises(KernelError, match="产品备份文件"):
        port.publish_private_object(Path(source), Path(target))
