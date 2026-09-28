"""产品状态静默窗口：第二宿主必须在任何Store或Provider构造之前拒绝。"""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import server
from harnessix.product_config.action_owner import product_action_runtime_lock
from harnessix.product_config.contracts import ProductConfigV2
from harnessix.product_config.state_owner import (
    ProductStateOwner,
    product_state_owner,
    state_owner_anchor,
)
from tests.product_config.conftest import write_config


async def test_second_product_startup_refuses_before_any_state_store(
    tmp_path: Path, config: ProductConfigV2, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    path = write_config(tmp_path / "config.json", config)
    monkeypatch.setenv("PRIMARY_API_KEY", "fixture-SECRET-CANARY")
    monkeypatch.setenv("BACKUP_API_KEY", "fixture-SECRET-CANARY")

    def forbidden_store(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("第二产品宿主在全状态Owner拒绝之前构造了配置Store")

    monkeypatch.setattr(server, "SQLiteProductRuntimeConfigStore", forbidden_store)
    with product_action_runtime_lock(state), pytest.raises(KernelError) as caught:
        await server.run_product_stdio(
            config_path=path,
            profile_id=None,
            workspace=workspace,
            state_directory=state,
            input_stream=io.BytesIO(),
            output_stream=io.BytesIO(),
        )
    assert caught.value.code in {"product_state_busy", "action_runtime_busy"}
    assert not (state / "product-config.db").exists()
    assert not (state / "sessions.db").exists()
    assert not (state / "session-auth").exists()


def test_owner_does_not_create_root_and_keeps_same_external_lock_inode(tmp_path: Path) -> None:
    root = tmp_path / "state"
    anchor = state_owner_anchor(root)
    with product_state_owner(root) as owner:
        owner.require(root)
        assert not root.exists()
        assert not anchor.is_relative_to(root)
        before = (anchor / ".lock").stat()
        with pytest.raises(KernelError) as caught, product_state_owner(root):
            pass
        assert caught.value.code == "product_state_busy"
    with product_state_owner(root) as reopened:
        reopened.require(root)
        after = (anchor / ".lock").stat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert (anchor / ".lock").read_bytes() == b"\0"


def test_borrowed_action_scope_does_not_reacquire_or_close_product_owner(tmp_path: Path) -> None:
    root = tmp_path / "state"
    with product_state_owner(root) as owner:
        for _ in range(2):
            with product_action_runtime_lock(root, root_owner=owner):
                owner.require(root)
        with pytest.raises(KernelError) as caught, product_state_owner(root):
            pass
        assert caught.value.code == "product_state_busy"
        owner.require(root)
    with pytest.raises(KernelError) as caught, product_action_runtime_lock(root, root_owner=owner):
        pass
    assert caught.value.code == "product_state_owner_invalid"


def test_owner_cannot_be_constructed_from_data_or_borrowed_by_another_root(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        ProductStateOwner()
    with product_state_owner(tmp_path / "state") as owner:
        with pytest.raises(KernelError) as caught:
            owner.require(tmp_path / "other")
        assert caught.value.code == "product_state_owner_invalid"


@pytest.mark.parametrize("alias", ["STATE", "stAte"])
def test_case_aliases_use_same_anchor_even_before_root_exists(tmp_path: Path, alias: str) -> None:
    root = tmp_path / "state"
    with product_state_owner(root) as owner:
        owner.require(tmp_path / alias)
        with pytest.raises(KernelError) as caught, product_state_owner(tmp_path / alias):
            pass
        assert caught.value.code == "product_state_busy"


def test_unicode_normalization_aliases_use_same_anchor(tmp_path: Path) -> None:
    assert state_owner_anchor(tmp_path / "caf\u00e9") == state_owner_anchor(tmp_path / "cafe\u0301")


def test_different_state_addresses_can_run_independently(tmp_path: Path) -> None:
    with product_state_owner(tmp_path / "one") as one, product_state_owner(tmp_path / "two") as two:
        one.require(one.state_root)
        two.require(two.state_root)


def test_action_exception_is_not_reclassified_as_owner_acquisition_failure(tmp_path: Path) -> None:
    error = KernelError("product_state_busy", "业务侧保留的原错误")
    with pytest.raises(KernelError) as caught, product_action_runtime_lock(tmp_path / "state"):
        raise error
    assert caught.value is error
    with product_state_owner(tmp_path / "state"):
        pass


def test_second_process_cannot_acquire_root_even_after_root_rename(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    script = (
        "from pathlib import Path; import sys; "
        "from harnessix.agent.errors import KernelError; "
        "from harnessix.product_config.state_owner import product_state_owner\n"
        "try:\n  with product_state_owner(Path(sys.argv[1])): pass\n"
        "except KernelError as error:\n  print(error.code); raise SystemExit(0)\n"
        "raise SystemExit(2)"
    )
    with product_state_owner(root) as owner:
        root.rename(tmp_path / "retained")
        owner.require(root)
        result = subprocess.run(
            [sys.executable, "-c", script, str(root)],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "product_state_busy"
        assert not root.exists()


async def test_process_death_releases_lock_without_deleting_anchor(tmp_path: Path) -> None:
    root = tmp_path / "state"
    script = (
        "from pathlib import Path; import sys; "
        "from harnessix.product_config.state_owner import product_state_owner\n"
        "with product_state_owner(Path(sys.argv[1])):\n"
        "  print('ready', flush=True); sys.stdin.buffer.read(1)\n"
    )
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(root),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        assert await asyncio.wait_for(child.stdout.readline(), 15) == b"ready\n"
        with pytest.raises(KernelError) as caught, product_state_owner(root):
            pass
        assert caught.value.code == "product_state_busy"
    finally:
        if child.returncode is None:
            child.kill()
        await asyncio.wait_for(child.wait(), 15)
    with product_state_owner(root) as owner:
        owner.require(root)
    assert (state_owner_anchor(root) / ".lock").is_file()


@pytest.mark.skipif(os.name != "posix", reason="需要POSIX权限及目录FD")
@pytest.mark.parametrize("target", ["anchor", "lock"])
def test_owner_rejects_unsafe_permissions_without_repair(tmp_path: Path, target: str) -> None:
    root = tmp_path / "state"
    with product_state_owner(root):
        pass
    anchor = state_owner_anchor(root)
    path = anchor if target == "anchor" else anchor / ".lock"
    mode = 0o755 if target == "anchor" else 0o644
    path.chmod(mode)
    with pytest.raises(KernelError) as caught, product_state_owner(root):
        pass
    assert caught.value.code == "product_state_owner_unavailable"
    assert path.stat().st_mode & 0o777 == mode
    assert not root.exists()


@pytest.mark.skipif(os.name != "posix", reason="需要POSIX权限及目录FD")
def test_owner_rejects_other_writable_parent(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    root = parent / "state"
    with pytest.raises(KernelError) as caught, product_state_owner(root):
        pass
    assert caught.value.code == "product_state_owner_unavailable"
    assert parent.stat().st_mode & 0o777 == 0o777
    assert not state_owner_anchor(root).exists()


@pytest.mark.skipif(os.name != "posix", reason="需要POSIX链接创建及目录FD")
@pytest.mark.parametrize("target", ["root", "anchor", "lock", "hardlink"])
def test_owner_rejects_links_before_touching_target(tmp_path: Path, target: str) -> None:
    root = tmp_path / "state"
    anchor = state_owner_anchor(root)
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    file = external / "file"
    file.write_bytes(b"original")
    file.chmod(0o600)
    if target == "root":
        root.symlink_to(external, target_is_directory=True)
    elif target == "anchor":
        anchor.symlink_to(external, target_is_directory=True)
    else:
        anchor.mkdir(mode=0o700)
        if target == "lock":
            (anchor / ".lock").symlink_to(file)
        else:
            os.link(file, anchor / ".lock")
    with pytest.raises(KernelError) as caught, product_state_owner(root):
        pass
    assert caught.value.code == "product_state_owner_unavailable"
    assert file.read_bytes() == b"original"


@pytest.mark.parametrize("body", [b"x", b"\0\0", b"unknown"])
def test_owner_does_not_overwrite_unknown_lock_body(tmp_path: Path, body: bytes) -> None:
    root = tmp_path / "state"
    with product_state_owner(root):
        pass
    lock = state_owner_anchor(root) / ".lock"
    lock.write_bytes(body)
    with pytest.raises(KernelError) as caught, product_state_owner(root):
        pass
    assert caught.value.code == "product_state_owner_unavailable"
    assert lock.read_bytes() == body


@pytest.mark.skipif(os.name != "posix", reason="需要POSIX目录及权限变化")
@pytest.mark.parametrize("target", ["anchor", "lock", "replace"])
def test_live_owner_refuses_anchor_or_lock_identity_drift(tmp_path: Path, target: str) -> None:
    root = tmp_path / "state"
    with product_state_owner(root) as owner:
        anchor = state_owner_anchor(root)
        lock = anchor / ".lock"
        if target == "anchor":
            anchor.chmod(0o755)
        elif target == "lock":
            lock.chmod(0o644)
        else:
            lock.unlink()
            lock.write_bytes(b"\0")
            lock.chmod(0o600)
        with pytest.raises(KernelError) as caught:
            owner.require(root)
        assert caught.value.code == "product_state_owner_invalid"


async def test_cancellation_settles_original_root_writer_before_releasing_owner(
    tmp_path: Path, config: ProductConfigV2, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "state"
    path = write_config(tmp_path / "config.json", config)
    monkeypatch.setenv("PRIMARY_API_KEY", "fixture-SECRET-CANARY")
    monkeypatch.setenv("BACKUP_API_KEY", "fixture-SECRET-CANARY")
    entered, release = asyncio.Event(), threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()
    original = server._private_root

    def paused(path: str | Path) -> Path:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        try:
            return original(path)
        finally:
            finished.set()

    monkeypatch.setattr(server, "_private_root", paused)
    task = asyncio.create_task(
        server.run_product_stdio(
            config_path=path,
            profile_id=None,
            workspace=workspace,
            state_directory=root,
            input_stream=io.BytesIO(),
            output_stream=io.BytesIO(),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(KernelError) as caught, product_state_owner(root):
            pass
        assert caught.value.code == "product_state_busy"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 15)
    assert finished.is_set()
    with product_state_owner(root) as owner:
        owner.require(root)
    assert not (root / "product-config.db").exists()
    assert not (root / "sessions.db").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="需要真实Darwin扩展ACL")
@pytest.mark.parametrize("target", ["anchor", "lock"])
def test_darwin_acl_is_rejected_without_chmod_repair(tmp_path: Path, target: str) -> None:
    root = tmp_path / "state"
    with product_state_owner(root):
        pass
    path = state_owner_anchor(root)
    if target == "lock":
        path = path / ".lock"
    result = subprocess.run(
        ["chmod", "+a", "everyone allow read", str(path)],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    with pytest.raises(KernelError) as caught, product_state_owner(root):
        pass
    assert caught.value.code == "product_state_owner_unavailable"
