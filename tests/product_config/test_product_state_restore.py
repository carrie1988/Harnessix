"""完整产品恢复：原备份、整体目录切换、耐久意图和明确回退，不使用单库替身。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import sys
import threading
from contextlib import closing
from uuid import UUID, uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import server as product
from harnessix.product_config.state_backup import backup_product_state, verify_product_backup
from harnessix.product_config.state_backup_files import PrivateStateTree, write_new
from harnessix.product_config.state_owner import state_owner_anchor
from tests.product_config.conftest import write_config
from tests.product_config.test_product_state_backup import complete_state as _complete_state

# 共享原真实产品Fixture，不复制第二套六库、Key和Artifact装配过程。
complete_state = _complete_state

pytestmark = pytest.mark.skipif(
    os.name not in {"posix", "nt"}, reason="完整产品恢复只验证POSIX和Windows原生端口"
)


async def test_restore_switches_all_state_and_preserves_previous_root(complete_state, tmp_path):
    from harnessix.product_config.state_restore import restore_product_state

    root, _ = complete_state
    backup = tmp_path / "backup"
    manifest = await backup_product_state(root, backup)
    old_key = (root / "session-auth/key.v1").read_bytes()
    added = b"original state after backup"
    name = "workspace-transactions/blobs/" + hashlib.sha256(added).hexdigest()
    with PrivateStateTree(root) as tree:
        write_new(tree, name, added)
    restore_id = uuid4()
    result = await restore_product_state(
        root, backup, restore_id=restore_id, confirm_backup_id=manifest.backup_id
    )
    assert result.status == "restored" and result.retained_previous_state
    assert not (root / name).exists()
    previous = root.parent / f".harnessix-restore-{restore_id}.previous"
    assert (previous / name).read_bytes() == added
    assert (root / "session-auth/key.v1").read_bytes() == old_key
    assert await verify_product_backup(root, backup) == manifest
    assert not (state_owner_anchor(root) / "restore-active.json").exists()


async def test_missing_root_restores_original_key_without_enrollment(complete_state, tmp_path):
    from harnessix.product_config.state_restore import restore_product_state

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    old_key = (root / "session-auth/key.v1").read_bytes()
    root.rename(tmp_path / "original-missing-root")
    result = await restore_product_state(
        root, tmp_path / "backup", restore_id=uuid4(), confirm_backup_id=manifest.backup_id
    )
    assert not result.retained_previous_state
    assert (root / "session-auth/key.v1").read_bytes() == old_key
    assert await verify_product_backup(root, tmp_path / "backup") == manifest


@pytest.mark.parametrize("damage", ["database", "key", "receipt"])
async def test_invalid_restore_never_changes_current_state(complete_state, tmp_path, damage):
    from harnessix.product_config.state_restore import restore_product_state

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    before = (root / "sessions.db").read_bytes(), (root / "session-auth/key.v1").read_bytes()
    if damage == "receipt":
        (state_owner_anchor(root) / f"backup-{manifest.backup_id}.json").unlink()
    else:
        relative = "sessions.db" if damage == "database" else "session-auth/key.v1"
        (tmp_path / "backup/state" / relative).write_bytes(b"invalid")
    with pytest.raises(KernelError):
        await restore_product_state(
            root, tmp_path / "backup", restore_id=uuid4(), confirm_backup_id=manifest.backup_id
        )
    assert before == (
        (root / "sessions.db").read_bytes(),
        (root / "session-auth/key.v1").read_bytes(),
    )
    assert not (state_owner_anchor(root) / "restore-active.json").exists()


@pytest.mark.parametrize(
    "phase",
    [
        "restore.after_intent",
        "restore.after_previous",
        "restore.after_publish",
        "restore.after_result",
    ],
)
async def test_interruption_blocks_startup_before_root_preparation_and_recovers(
    complete_state, tmp_path, monkeypatch, config, phase
):
    from harnessix.product_config.state_restore import (
        recover_product_state_restore,
        restore_product_state,
    )

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    restore_id = uuid4()

    def fault(current):
        if current == phase:
            raise KernelError("injected_restore_failure", "受控恢复中断")

    with pytest.raises(KernelError, match="受控恢复中断"):
        await restore_product_state(
            root,
            tmp_path / "backup",
            restore_id=restore_id,
            confirm_backup_id=manifest.backup_id,
            fault=fault,
        )
    assert (state_owner_anchor(root) / "restore-active.json").exists()

    async def forbidden(**_kwargs):
        pytest.fail("未决恢复期间不得准备Root、创建Key或初始化Store")

    monkeypatch.setattr(product, "_validated_runtime_paths", forbidden)
    with pytest.raises(KernelError) as error:
        await product.run_product_stdio(
            config_path=write_config(tmp_path / "config.json", config),
            profile_id=None,
            workspace=tmp_path / "workspace",
            state_directory=root,
            input_stream=io.BytesIO(),
            output_stream=io.BytesIO(),
        )
    assert error.value.code == "product_state_restore_pending"
    result = await recover_product_state_restore(
        root, confirm_restore_id=restore_id, mode="complete"
    )
    assert result.status == "restored"
    assert await verify_product_backup(root, tmp_path / "backup") == manifest


@pytest.mark.parametrize(
    "phase", ["restore.after_intent", "restore.after_previous", "restore.after_publish"]
)
async def test_explicit_rollback_returns_original_root_identity(complete_state, tmp_path, phase):
    from harnessix.product_config.state_restore import (
        recover_product_state_restore,
        restore_product_state,
    )

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    identity = root.stat().st_dev, root.stat().st_ino
    restore_id = uuid4()

    def fault(current):
        if current == phase:
            raise KernelError("injected_restore_failure", "受控恢复中断")

    with pytest.raises(KernelError):
        await restore_product_state(
            root,
            tmp_path / "backup",
            restore_id=restore_id,
            confirm_backup_id=manifest.backup_id,
            fault=fault,
        )
    result = await recover_product_state_restore(
        root, confirm_restore_id=restore_id, mode="rollback"
    )
    assert result.status == "rolled_back"
    assert identity == (root.stat().st_dev, root.stat().st_ino)
    assert not (state_owner_anchor(root) / "restore-active.json").exists()


@pytest.mark.parametrize(
    "phase", ["restore.after_intent", "restore.after_previous", "restore.after_publish"]
)
async def test_real_process_hard_exit_recovers_original_journal(complete_state, tmp_path, phase):
    from harnessix.product_config.state_restore import recover_product_state_restore

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    restore_id = uuid4()
    program = """
import asyncio, os, sys
from pathlib import Path
from uuid import UUID
from harnessix.product_config.state_restore import restore_product_state
def fault(phase):
    if phase == sys.argv[5]:
        os._exit(19)
asyncio.run(restore_product_state(Path(sys.argv[1]), Path(sys.argv[2]),
    restore_id=UUID(sys.argv[3]), confirm_backup_id=UUID(sys.argv[4]), fault=fault))
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        str(root),
        str(tmp_path / "backup"),
        str(restore_id),
        str(manifest.backup_id),
        phase,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(15):
            _output, errors = await child.communicate()
        assert child.returncode == 19, errors.decode()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    result = await recover_product_state_restore(
        root, confirm_restore_id=restore_id, mode="complete"
    )
    assert result.status == "restored"
    assert await verify_product_backup(root, tmp_path / "backup") == manifest


@pytest.mark.parametrize("original_bytes", [b"broken original database", b""])
async def test_restore_can_replace_corrupt_current_database_without_repairing_previous(
    complete_state, tmp_path, original_bytes
):
    from harnessix.product_config.state_restore import restore_product_state

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    (root / "sessions.db").write_bytes(original_bytes)
    restore_id = uuid4()
    await restore_product_state(
        root, tmp_path / "backup", restore_id=restore_id, confirm_backup_id=manifest.backup_id
    )
    previous = root.parent / f".harnessix-restore-{restore_id}.previous"
    assert (previous / "sessions.db").read_bytes() == original_bytes
    assert await verify_product_backup(root, tmp_path / "backup") == manifest


async def test_completed_request_never_overwrites_newer_state(complete_state, tmp_path):
    from harnessix.product_config.state_restore import (
        recover_product_state_restore,
        restore_product_state,
    )

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    restore_id = uuid4()
    original = await restore_product_state(
        root, tmp_path / "backup", restore_id=restore_id, confirm_backup_id=manifest.backup_id
    )
    name = "workspace-transactions/blobs/" + hashlib.sha256(b"newer state").hexdigest()
    with PrivateStateTree(root) as tree:
        write_new(tree, name, b"newer state")
    repeated = await restore_product_state(
        root, tmp_path / "backup", restore_id=restore_id, confirm_backup_id=manifest.backup_id
    )
    assert repeated == original
    assert (
        await recover_product_state_restore(root, confirm_restore_id=restore_id, mode="complete")
        == original
    )
    assert (root / name).read_bytes() == b"newer state"


async def test_cli_restore_and_recover_keep_stable_request_id(complete_state, tmp_path):
    from harnessix.product_config.state_backup_cli import state_main

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    restore_id = uuid4()
    import contextlib

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        await asyncio.to_thread(
            state_main,
            [
                "restore",
                "--state-directory",
                str(root),
                "--backup-directory",
                str(tmp_path / "backup"),
                "--restore-id",
                str(restore_id),
                "--confirm-backup",
                str(manifest.backup_id),
            ],
        )
    result = json.loads(output.getvalue())
    assert result["restore_id"] == str(restore_id) and result["status"] == "restored"
    assert str(root) not in output.getvalue()
    recovered = io.StringIO()
    with contextlib.redirect_stdout(recovered):
        await asyncio.to_thread(
            state_main,
            [
                "recover",
                "--state-directory",
                str(root),
                "--confirm-restore",
                str(restore_id),
                "--mode",
                "complete",
            ],
        )
    assert json.loads(recovered.getvalue()) == result


async def test_terminal_record_does_not_clear_pending_guard_for_lost_root(complete_state, tmp_path):
    from harnessix.product_config.state_restore import (
        recover_product_state_restore,
        restore_product_state,
    )

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    restore_id = uuid4()

    def fault(phase):
        if phase == "restore.after_result":
            raise KernelError("injected_restore_failure", "受控恢复中断")

    with pytest.raises(KernelError):
        await restore_product_state(
            root,
            tmp_path / "backup",
            restore_id=restore_id,
            confirm_backup_id=manifest.backup_id,
            fault=fault,
        )
    root.rename(tmp_path / "unexpected-lost-root")
    with pytest.raises(KernelError) as error:
        await recover_product_state_restore(root, confirm_restore_id=restore_id, mode="complete")
    assert error.value.code == "product_restore_state_changed"
    assert not root.exists()
    assert (state_owner_anchor(root) / "restore-active.json").exists()


@pytest.mark.parametrize("phase", ["copy", "intent"])
async def test_repeated_cancel_settles_original_worker_before_owner_release(
    complete_state, tmp_path, phase
):
    from harnessix.product_config.state_owner import product_state_owner
    from harnessix.product_config.state_restore import (
        recover_product_state_restore,
        restore_product_state,
    )

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    restore_id = uuid4()
    entered, release = threading.Event(), threading.Event()
    paused = False

    def fault(current):
        nonlocal paused
        selected = (
            current.startswith("restore.after_copy:")
            if phase == "copy"
            else current == "restore.after_intent"
        )
        if selected and not paused:
            paused = True
            entered.set()
            assert release.wait(10)

    task = asyncio.create_task(
        restore_product_state(
            root,
            tmp_path / "backup",
            restore_id=restore_id,
            confirm_backup_id=manifest.backup_id,
            fault=fault,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(KernelError) as error:
            with product_state_owner(root):
                pass
        assert error.value.code == "product_state_busy"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    if phase == "intent":
        assert (state_owner_anchor(root) / "restore-active.json").exists()
        await recover_product_state_restore(root, confirm_restore_id=restore_id, mode="rollback")
    else:
        assert not (state_owner_anchor(root) / "restore-active.json").exists()
        assert not (root.parent / f".harnessix-restore-{restore_id}.candidate").exists()
    with product_state_owner(root):
        pass


@pytest.mark.parametrize("target_role", ["previous", "root"])
async def test_native_rename_confirmation_loss_uses_actual_identity_not_retry(
    complete_state, tmp_path, monkeypatch, target_role
):
    from harnessix.product_config import state_restore_flow
    from harnessix.product_config.state_restore import (
        recover_product_state_restore,
        restore_product_state,
    )

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    restore_id = uuid4()
    previous = root.parent / f".harnessix-restore-{restore_id}.previous"
    original = state_restore_flow.publish_tree
    calls = []
    lost = False

    def publish_then_lose_confirmation(source, target):
        nonlocal lost
        original(source, target)
        calls.append(target)
        selected = previous if target_role == "previous" else root
        if target == selected and not lost:
            lost = True
            raise OSError("namespace confirmation unavailable")

    monkeypatch.setattr(state_restore_flow, "publish_tree", publish_then_lose_confirmation)
    with pytest.raises(KernelError):
        await restore_product_state(
            root, tmp_path / "backup", restore_id=restore_id, confirm_backup_id=manifest.backup_id
        )
    await recover_product_state_restore(root, confirm_restore_id=restore_id, mode="complete")
    assert calls.count(previous) == calls.count(root) == 1
    assert await verify_product_backup(root, tmp_path / "backup") == manifest


async def _interrupt_restore(root, backup, restore_id, manifest, phase):
    """通过正式恢复入口制造单一已声明中断，不伪造计划或目录布局。"""
    from harnessix.product_config.state_restore import restore_product_state

    def fault(current):
        if current == phase:
            raise KernelError("injected_restore_failure", "受控恢复中断")

    with pytest.raises(KernelError, match="受控恢复中断"):
        await restore_product_state(
            root, backup, restore_id=restore_id, confirm_backup_id=manifest.backup_id, fault=fault
        )


@pytest.mark.parametrize("damage", ["confirmation", "current-key", "missing-key"])
async def test_wrong_confirmation_or_current_key_preserves_existing_root(
    complete_state, tmp_path, damage
):
    from harnessix.product_config.state_restore import restore_product_state

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    key = root / "session-auth/key.v1"
    if damage == "current-key":
        key.write_bytes(b"wrong original key")
    elif damage == "missing-key":
        key.unlink()
    original = (root / "sessions.db").read_bytes(), key.read_bytes() if key.exists() else None
    identity = root.stat().st_dev, root.stat().st_ino
    restore_id = uuid4()
    with pytest.raises(KernelError):
        await restore_product_state(
            root,
            tmp_path / "backup",
            restore_id=restore_id,
            confirm_backup_id=uuid4() if damage == "confirmation" else manifest.backup_id,
        )
    assert identity == (root.stat().st_dev, root.stat().st_ino)
    assert original == (
        (root / "sessions.db").read_bytes(),
        key.read_bytes() if key.exists() else None,
    )
    assert not (state_owner_anchor(root) / "restore-active.json").exists()
    assert not (root.parent / f".harnessix-restore-{restore_id}.candidate").exists()


@pytest.mark.parametrize(
    "phase", ["restore.rollback_after_candidate", "restore.rollback_after_previous"]
)
async def test_interrupted_rollback_is_sticky_and_returns_original_identity(
    complete_state, tmp_path, phase
):
    from harnessix.product_config.state_restore import recover_product_state_restore

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    original = root.stat().st_dev, root.stat().st_ino
    restore_id = uuid4()
    await _interrupt_restore(
        root, tmp_path / "backup", restore_id, manifest, "restore.after_publish"
    )

    def fault(current):
        if current == phase:
            raise KernelError("injected_rollback_failure", "受控回退中断")

    with pytest.raises(KernelError, match="受控回退中断"):
        await recover_product_state_restore(
            root, confirm_restore_id=restore_id, mode="rollback", fault=fault
        )
    with pytest.raises(KernelError) as error:
        await recover_product_state_restore(root, confirm_restore_id=restore_id, mode="complete")
    assert error.value.code == "product_restore_rollback_pending"
    result = await recover_product_state_restore(
        root, confirm_restore_id=restore_id, mode="rollback"
    )
    assert result.status == "rolled_back"
    assert original == (root.stat().st_dev, root.stat().st_ino)
    assert not (state_owner_anchor(root) / "restore-active.json").exists()


@pytest.mark.parametrize("phase", ["restore.after_intent", "restore.after_publish"])
async def test_missing_original_root_rollback_returns_missing_state(
    complete_state, tmp_path, phase
):
    from harnessix.product_config.state_restore import recover_product_state_restore

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    root.rename(tmp_path / "retained-original")
    restore_id = uuid4()
    await _interrupt_restore(root, tmp_path / "backup", restore_id, manifest, phase)
    result = await recover_product_state_restore(
        root, confirm_restore_id=restore_id, mode="rollback"
    )
    assert result.status == "rolled_back" and not result.retained_previous_state
    assert not root.exists()
    assert not (state_owner_anchor(root) / "restore-active.json").exists()


@pytest.mark.parametrize("damage", ["plan", "pointer", "receipt", "candidate"])
async def test_pending_recovery_rejects_tampered_source_or_foreign_directory(
    complete_state, tmp_path, damage
):
    from harnessix.product_config.state_restore import recover_product_state_restore

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    restore_id = uuid4()
    await _interrupt_restore(
        root, tmp_path / "backup", restore_id, manifest, "restore.after_intent"
    )
    identity = root.stat().st_dev, root.stat().st_ino
    anchor = state_owner_anchor(root)
    if damage == "plan":
        plan = anchor / f"restore-{restore_id}/plan.json"
        plan.write_bytes(plan.read_bytes() + b" ")
    elif damage == "pointer":
        pointer = anchor / "restore-active.json"
        changed = json.loads(pointer.read_bytes())
        changed["plan_sha256"] = "0" * 64
        pointer.write_text(json.dumps(changed))
    elif damage == "receipt":
        (anchor / f"backup-{manifest.backup_id}.json").unlink()
    else:
        candidate = root.parent / f".harnessix-restore-{restore_id}.candidate"
        candidate.rename(tmp_path / "retained-candidate")
        candidate.mkdir(mode=0o700)
        (candidate / "foreign.txt").write_bytes(b"keep foreign bytes")
    with pytest.raises(KernelError):
        await recover_product_state_restore(root, confirm_restore_id=restore_id, mode="complete")
    assert identity == (root.stat().st_dev, root.stat().st_ino)
    assert (anchor / "restore-active.json").exists()
    if damage == "candidate":
        assert (candidate / "foreign.txt").read_bytes() == b"keep foreign bytes"


async def test_terminal_record_revalidates_snapshot_before_clearing_guard(complete_state, tmp_path):
    from harnessix.product_config.state_restore import recover_product_state_restore

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    restore_id = uuid4()
    await _interrupt_restore(
        root, tmp_path / "backup", restore_id, manifest, "restore.after_result"
    )
    (root / "sessions.db").write_bytes(b"damaged after durable result")
    with pytest.raises(KernelError):
        await recover_product_state_restore(root, confirm_restore_id=restore_id, mode="complete")
    assert (state_owner_anchor(root) / "restore-active.json").exists()
    assert (root / "sessions.db").read_bytes() == b"damaged after durable result"


async def test_pending_recovery_uses_pinned_manifest_without_original_bundle(
    complete_state, tmp_path
):
    from harnessix.product_config.state_restore import recover_product_state_restore

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    restore_id = uuid4()
    await _interrupt_restore(
        root, tmp_path / "backup", restore_id, manifest, "restore.after_previous"
    )
    (tmp_path / "backup").rename(tmp_path / "retained-original-backup")
    result = await recover_product_state_restore(
        root, confirm_restore_id=restore_id, mode="complete"
    )
    assert result.status == "restored"
    assert await verify_product_backup(root, tmp_path / "retained-original-backup") == manifest


@pytest.mark.parametrize("committed", [False, True])
async def test_activation_failure_cleans_only_uncommitted_candidate(
    complete_state, tmp_path, monkeypatch, committed
):
    from harnessix.product_config import state_restore_journal
    from harnessix.product_config.state_restore import (
        recover_product_state_restore,
        restore_product_state,
    )

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    restore_id = uuid4()
    original = state_restore_journal.publish_tree
    failed = False

    def fail_active_publication(source, target):
        nonlocal failed
        if target.name == "restore-active.json" and not failed:
            failed = True
            if committed:
                original(source, target)
            raise OSError("controlled active publication failure")
        original(source, target)

    monkeypatch.setattr(state_restore_journal, "publish_tree", fail_active_publication)
    identity = root.stat().st_dev, root.stat().st_ino
    with pytest.raises(KernelError):
        await restore_product_state(
            root, tmp_path / "backup", restore_id=restore_id, confirm_backup_id=manifest.backup_id
        )
    assert identity == (root.stat().st_dev, root.stat().st_ino)
    candidate = root.parent / f".harnessix-restore-{restore_id}.candidate"
    assert (state_owner_anchor(root) / "restore-active.json").exists() == committed
    assert candidate.exists() == committed
    if committed:
        result = await recover_product_state_restore(
            root, confirm_restore_id=restore_id, mode="complete"
        )
        assert result.status == "restored"


@pytest.mark.parametrize("mode", ["unknown", [], None])
async def test_invalid_recover_mode_does_not_create_state_or_owner(tmp_path, mode):
    from harnessix.product_config.state_restore import recover_product_state_restore

    root = tmp_path / "missing-state"
    with pytest.raises(KernelError) as error:
        await recover_product_state_restore(root, confirm_restore_id=uuid4(), mode=mode)
    assert error.value.code == "product_restore_mode_invalid"
    assert not root.exists() and not state_owner_anchor(root).exists()


async def test_prepare_timeout_cleans_private_candidate_and_preserves_root(
    complete_state, tmp_path, monkeypatch
):
    from harnessix.product_config.state_restore import restore_product_state
    from harnessix.session import maintenance_io

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    restore_id = uuid4()
    original = root.stat().st_dev, root.stat().st_ino
    clock = [0.0]
    monkeypatch.setattr(maintenance_io, "monotonic", lambda: clock[0])

    def expire_after_copy(phase):
        if phase.startswith("restore.after_copy:"):
            # 只推进维护线程的合作时钟，不依赖测试机速度或任意sleep。
            clock[0] = 1.0

    with pytest.raises(KernelError) as error:
        await restore_product_state(
            root,
            tmp_path / "backup",
            restore_id=restore_id,
            confirm_backup_id=manifest.backup_id,
            budget_seconds=0.08,
            fault=expire_after_copy,
        )
    assert error.value.code == "maintenance_io_timeout"
    assert original == (root.stat().st_dev, root.stat().st_ino)
    assert not (state_owner_anchor(root) / "restore-active.json").exists()
    assert not (root.parent / f".harnessix-restore-{restore_id}.candidate").exists()


@pytest.mark.parametrize("budget", [0.0, -1.0, float("nan"), float("inf"), 301.0])
async def test_invalid_budget_never_creates_root_or_anchor(tmp_path, budget):
    from harnessix.product_config.state_restore import restore_product_state

    root = tmp_path / "missing-state"
    with pytest.raises(KernelError) as error:
        await restore_product_state(
            root,
            tmp_path / "missing-backup",
            restore_id=uuid4(),
            confirm_backup_id=uuid4(),
            budget_seconds=budget,
        )
    assert error.value.code == "product_backup_budget_invalid"
    assert not root.exists() and not state_owner_anchor(root).exists()


async def test_restored_product_reopens_original_thread_and_artifact(
    complete_state, tmp_path, config, monkeypatch
):
    from harnessix.product_config.state_restore import restore_product_state
    from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport

    root, _ = complete_state
    with closing(sqlite3.connect(root / "sessions.db")) as database:
        thread_id, artifact_id = database.execute(
            "SELECT thread_id, artifact_id FROM agent_artifacts LIMIT 1"
        ).fetchone()
    manifest = await backup_product_state(root, tmp_path / "backup")
    await restore_product_state(
        root, tmp_path / "backup", restore_id=uuid4(), confirm_backup_id=manifest.backup_id
    )
    observed = []

    async def read_restored(server, _input, _output):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            listing = await client.list_threads()
            assert [thread.thread_id for thread in listing.threads] == [UUID(thread_id)]
            assert (await client.get_thread(UUID(thread_id))).latest_turn.status == "completed"
            page = await client.read_artifact(UUID(thread_id), UUID(artifact_id), limit=3)
            assert page.text and page.artifact.records == 300
            observed.append(page.artifact.artifact_id)
        finally:
            await client.close()

    monkeypatch.setattr(product, "run_stdio", read_restored)
    await product.run_product_stdio(
        config_path=write_config(tmp_path / "restored-config.json", config),
        profile_id=None,
        workspace=tmp_path / "workspace",
        state_directory=root,
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )
    assert observed == [UUID(artifact_id)]
