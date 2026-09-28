"""全状态Owner的Windows原生DACL、Junction和硬链接验证；其他平台不冒充验收。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.state_owner import product_state_owner, state_owner_anchor

pytestmark = pytest.mark.skipif(os.name != "nt", reason="需要真实Windows原生Handle和DACL")


@pytest.mark.parametrize("target", ["anchor", "lock"])
def test_world_read_dacl_is_rejected_without_silent_repair(tmp_path: Path, target: str) -> None:
    root = tmp_path / "state"
    with product_state_owner(root):
        pass
    path = state_owner_anchor(root)
    if target == "lock":
        path /= ".lock"
    result = subprocess.run(
        ["icacls", str(path), "/grant", "*S-1-1-0:(R)", "/q"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    with pytest.raises(KernelError) as caught, product_state_owner(root):
        pass
    assert caught.value.code == "product_state_owner_unavailable"
    assert not root.exists()


def test_native_lock_hardlink_is_not_overwritten(tmp_path: Path) -> None:
    root = tmp_path / "state"
    with product_state_owner(root):
        pass
    lock = state_owner_anchor(root) / ".lock"
    os.link(lock, tmp_path / "alias")
    with pytest.raises(KernelError) as caught, product_state_owner(root):
        pass
    assert caught.value.code == "product_state_owner_unavailable"
    assert lock.read_bytes() == b"\0"


@pytest.mark.parametrize("target", ["root", "anchor", "parent"])
def test_junction_never_creates_owner_in_target(tmp_path: Path, target: str) -> None:
    root = tmp_path / "state"
    external = tmp_path / "external"
    external.mkdir()
    path = root if target == "root" else state_owner_anchor(root)
    if target == "parent":
        path = tmp_path / "parent-alias"
        root = path / "missing" / "state"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(path), str(external)],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    with pytest.raises(KernelError) as caught, product_state_owner(root):
        pass
    assert caught.value.code == "product_state_owner_unavailable"
    assert not list(external.iterdir())


def test_missing_parents_are_created_through_private_native_directory_port(tmp_path: Path) -> None:
    root = tmp_path / "first" / "second" / "state"
    with product_state_owner(root) as owner:
        owner.require(root)
    assert root.parent.is_dir()
    assert not root.exists()
