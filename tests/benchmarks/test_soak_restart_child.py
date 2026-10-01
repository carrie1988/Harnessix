"""真实产品stdio子进程的正常关闭和受控硬退出。"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
from pathlib import Path
from threading import Event
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.action_store import SQLiteProductRuntimeConfigStore
from harnessix.product_config.codec import canonical_product_config_bytes
from harnessix.product_config.contracts import (
    EnvironmentSecretSourceConfig,
    ModelCapabilities,
    ModelProfile,
    ProductConfigV2,
    ProviderDefinition,
    SecretReference,
)
from harnessix.sdk.agent_client import AgentClient, AgentSDKError
from harnessix.sdk.subprocess import SubprocessAgentTransport
from scripts import soak_restart, soak_restart_child
from scripts.soak_restart_proof import SoakRestartChildResult


async def test_private_marker_is_invisible_until_complete_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "crash.ack"
    entered, release = Event(), Event()
    original_write = os.write

    def hold_write(descriptor: int, body: bytes) -> int:
        # 在真实写入前设置屏障，不靠休眠猜测最终名的可见窗口。
        entered.set()
        if not release.wait(10):
            raise AssertionError("写入屏障未释放")
        return original_write(descriptor, body)

    monkeypatch.setattr(soak_restart_child.os, "write", hold_write)
    publisher = asyncio.create_task(
        asyncio.to_thread(soak_restart_child._write_private, target, b"ACK\n")
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10), "未进入真实写入屏障"
        visible = await asyncio.to_thread(target.is_file)
        body = await asyncio.to_thread(target.read_bytes) if visible else None
        assert (visible, body) == (False, None)
    finally:
        release.set()
        await publisher
    assert await asyncio.to_thread(target.read_bytes) == b"ACK\n"


def test_private_marker_completes_short_writes_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "crash.ack"
    original_write = os.write
    writes = []

    def short_write(descriptor: int, body: memoryview) -> int:
        assert not target.exists()
        if os.name == "posix":
            assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o600
        writes.append(bytes(body))
        return original_write(descriptor, body[:1])

    monkeypatch.setattr(soak_restart_child.os, "write", short_write)
    soak_restart_child._write_private(target, b"ACK\n")
    assert writes == [b"ACK\n", b"CK\n", b"K\n", b"\n"]
    assert target.read_bytes() == b"ACK\n"
    assert list(tmp_path.iterdir()) == [target]
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_private_marker_closes_synced_file_before_atomic_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "crash.ack"
    original_fsync, original_close, original_link = os.fsync, os.close, os.link
    events = []
    file_descriptors = []

    def observe_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            assert not target.exists()
            file_descriptors.append(descriptor)
            events.append("file-fsync")
        else:
            assert target.read_bytes() == b"ACK\n"
            events.append("directory-fsync")
        original_fsync(descriptor)

    def observe_close(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            events.append("file-close")
        original_close(descriptor)

    def observe_link(source: Path, destination: Path) -> None:
        assert source.parent == destination.parent == tmp_path
        assert source.read_bytes() == b"ACK\n"
        assert not target.exists()
        with pytest.raises(OSError):
            os.fstat(file_descriptors[0])
        events.append("link")
        original_link(source, destination)
        assert target.read_bytes() == b"ACK\n"

    monkeypatch.setattr(soak_restart_child.os, "fsync", observe_fsync)
    monkeypatch.setattr(soak_restart_child.os, "close", observe_close)
    monkeypatch.setattr(soak_restart_child.os, "link", observe_link)
    soak_restart_child._write_private(target, b"ACK\n")
    assert events == ["file-fsync", "file-close", "link"] + (
        ["directory-fsync"] if os.name == "posix" else []
    )
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("existing", [False, True], ids=["absent", "preserved"])
@pytest.mark.parametrize("fault", ["write", "zero-write", "negative-write", "fsync", "link"])
def test_private_marker_failure_never_publishes_partial_or_overwrites_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool, fault: str
) -> None:
    target = tmp_path / "crash.ack"
    sentinel = tmp_path / "unrelated.bin"
    sentinel.write_bytes(b"unrelated")
    if existing:
        target.write_bytes(b"original")
    before = target.stat() if existing else None
    original_write = os.write
    written = False
    descriptors = []

    def fail_write(descriptor: int, body: memoryview) -> int:
        nonlocal written
        descriptors.append(descriptor)
        if fault == "zero-write":
            return 0
        if fault == "negative-write":
            return -1
        if written:
            raise OSError("固定写入故障")
        written = True
        return original_write(descriptor, body[:1])

    def fail_fsync(descriptor: int) -> None:
        descriptors.append(descriptor)
        raise OSError("固定文件同步故障")

    def fail_link(source: Path, destination: Path) -> None:
        assert source.read_bytes() == b"ACK\n"
        raise OSError("固定原子发布故障")

    if fault in {"write", "zero-write", "negative-write"}:
        monkeypatch.setattr(soak_restart_child.os, "write", fail_write)
    elif fault == "fsync":
        monkeypatch.setattr(soak_restart_child.os, "fsync", fail_fsync)
    else:
        monkeypatch.setattr(soak_restart_child.os, "link", fail_link)
    with pytest.raises(OSError):
        soak_restart_child._write_private(target, b"ACK\n")
    if existing:
        assert target.stat() == before
        assert target.read_bytes() == b"original"
    else:
        assert not target.exists()
    assert sentinel.read_bytes() == b"unrelated"
    assert set(tmp_path.iterdir()) == ({target, sentinel} if existing else {sentinel})
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("kind", ["file", "hardlink", "directory"])
def test_private_marker_atomic_link_preserves_existing_target(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "crash.ack"
    original = tmp_path / "original.bin"
    original.write_bytes(b"original")
    if kind == "directory":
        target.mkdir()
    elif kind == "hardlink":
        os.link(original, target)
    else:
        target.write_bytes(b"original")
    before = target.stat()
    with pytest.raises(OSError):
        soak_restart_child._write_private(target, b"ACK\n")
    assert target.stat() == before
    assert original.read_bytes() == b"original"
    assert target.is_dir() if kind == "directory" else target.read_bytes() == b"original"
    assert set(tmp_path.iterdir()) == {target, original}


def test_private_marker_rejects_target_created_at_link_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "crash.ack"
    original_link = os.link

    def create_competing_target(source: Path, destination: Path) -> None:
        destination.write_bytes(b"competing-publisher")
        original_link(source, destination)

    monkeypatch.setattr(soak_restart_child.os, "link", create_competing_target)
    with pytest.raises(FileExistsError):
        soak_restart_child._write_private(target, b"ACK\n")
    assert target.read_bytes() == b"competing-publisher"
    assert list(tmp_path.iterdir()) == [target]


def test_private_marker_does_not_remove_unowned_temporary_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "crash.ack"
    nonce = UUID(int=1)
    temporary = tmp_path / f".{target.name}.{nonce.hex}.tmp"
    temporary.write_bytes(b"foreign-temporary")
    before = temporary.stat()
    monkeypatch.setattr(soak_restart_child, "uuid4", lambda: nonce)
    with pytest.raises(FileExistsError):
        soak_restart_child._write_private(target, b"ACK\n")
    assert temporary.stat() == before
    assert temporary.read_bytes() == b"foreign-temporary"
    assert list(tmp_path.iterdir()) == [temporary]


@pytest.mark.skipif(os.name != "posix", reason="POSIX目录fsync故障边界")
def test_private_marker_directory_sync_failure_leaves_only_complete_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "crash.ack"
    original_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("固定目录同步故障")
        original_fsync(descriptor)

    monkeypatch.setattr(soak_restart_child.os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError):
        soak_restart_child._write_private(target, b"ACK\n")
    # 发布已发生，不能谎称未发布或回删完整目标；这里只是不保证目录持久化。
    assert target.read_bytes() == b"ACK\n"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("body", [b"", b"A", b"ACK", b"ACK\nextra", b"ack\n"])
async def test_published_invalid_ack_is_rejected_after_one_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    target = tmp_path / "crash.ack"
    await asyncio.to_thread(soak_restart_child._write_private, target, body)
    original_read = Path.read_bytes
    reads = []

    def observe_read(path: Path) -> bytes:
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", observe_read)
    with pytest.raises(KernelError) as error:
        await soak_restart._wait_ack(tmp_path)
    assert error.value.code == "soak_restart_crash_invalid"
    assert reads == [target]


def _config(path: Path) -> Path:
    secret = SecretReference(name="soak-key", version="v1")
    config = ProductConfigV2(
        active_profile="soak",
        secret_sources=(
            EnvironmentSecretSourceConfig(secret=secret, environment_variable="HARNESSIX_SOAK_KEY"),
        ),
        providers=(
            ProviderDefinition(
                provider_id="soak",
                kind="openai_chat",
                base_url="https://api.openai.test/v1",
                credential=secret,
            ),
        ),
        profiles=(
            ModelProfile(
                profile_id="soak",
                provider_id="soak",
                model="soak-offline",
                capabilities=ModelCapabilities(),
            ),
        ),
    )
    path.write_bytes(canonical_product_config_bytes(config))
    path.chmod(0o600)
    return path


async def _wait_marker(path: Path) -> None:
    async with asyncio.timeout(20):
        while not path.is_file():  # noqa: ASYNC110, ASYNC240
            await asyncio.sleep(0.005)


def _client(config: Path, workspace: Path, state: Path, gate: Path) -> AgentClient:
    gate.mkdir(mode=0o700)
    command = (
        sys.executable,
        str(Path(__file__).parents[2] / "scripts" / "soak_restart_child.py"),
        str(config),
        str(workspace),
        str(state),
        str(gate),
    )
    return AgentClient(SubprocessAgentTransport(command))


async def test_real_product_restart_child_closes_and_hard_exits_without_turn(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    config = _config(tmp_path / "config.json")

    first = _client(config, workspace, state, tmp_path / "gate-one")
    try:
        async with asyncio.timeout(30):
            await first.initialize()
            thread = await first.create_thread(str(workspace), request_id="soak-first")
            page = await first.list_threads(limit=50)
        assert [item.thread_id for item in page.threads] == [thread.thread_id]
    finally:
        await first.close()
    result_path = tmp_path / "gate-one" / "child-result.json"
    result = SoakRestartChildResult.model_validate_json(result_path.read_bytes())
    assert result.rss.rss_bytes > 0
    with SQLiteProductRuntimeConfigStore(state / "product-config.db") as store:
        assert len(store.action_recovery_scans()) == 1
        assert len(store.action_recovery_reports()) == 1

    second_gate = tmp_path / "gate-two"
    second = _client(config, workspace, state, second_gate)
    try:
        async with asyncio.timeout(30):
            await second.initialize()
            page = await second.list_threads(limit=50)
        assert [item.thread_id for item in page.threads] == [thread.thread_id]
        (second_gate / "crash.request").write_bytes(b"CRASH\n")
        await _wait_marker(second_gate / "crash.ack")
        with pytest.raises(AgentSDKError) as error:
            async with asyncio.timeout(30):
                await second.list_threads(limit=50)
        assert error.value.code == "server_closed"
    finally:
        await second.close()
    assert (second_gate / "crash.ack").read_bytes() == b"ACK\n"
    assert not (second_gate / "child-result.json").exists()
    with SQLiteProductRuntimeConfigStore(state / "product-config.db") as store:
        assert [scan.owner_generation for scan in store.action_recovery_scans()] == [1, 2]
        assert len(store.action_recovery_reports()) == 2


@pytest.mark.skipif(os.name != "nt", reason="原生Windows验证普通mkdir与私有Root拒绝")
async def test_windows_child_refuses_precreated_state_without_repair(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    sentinel = state / "preserved.bin"
    sentinel.write_bytes(b"original fixture\n")
    before = (
        await asyncio.to_thread(
            subprocess.run, ["icacls", str(state)], capture_output=True, check=True
        )
    ).stdout
    config = _config(tmp_path / "config.json")
    gate = tmp_path / "gate-rejected"
    client = _client(config, workspace, state, gate)
    try:
        with pytest.raises(AgentSDKError) as error:
            async with asyncio.timeout(30):
                await client.initialize()
        assert error.value.code == "server_closed"
    finally:
        await client.close()
    # 原始低敏失败码定位真实组合根，不用模拟OS或放宽ACL把夹具变成成功。
    assert (gate / "child-failure.txt").read_bytes() == b"KernelError:product_state_invalid\n"
    assert not (gate / "child-result.json").exists()
    assert sentinel.read_bytes() == b"original fixture\n"
    assert {path.name for path in state.iterdir()} == {sentinel.name}
    after = (
        await asyncio.to_thread(
            subprocess.run, ["icacls", str(state)], capture_output=True, check=True
        )
    ).stdout
    assert after == before
