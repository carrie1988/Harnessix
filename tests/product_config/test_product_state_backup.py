"""完整产品状态备份：真实默认装配、原来源认证和跨Store引用，不使用单库替身。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import sqlite3
import threading
from contextlib import closing, contextmanager

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import utc_now
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config import server as product
from harnessix.product_config.runtime import build_provider_bundle
from harnessix.product_config.state_owner import product_state_owner, state_owner_anchor
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from tests.agent.helpers import answer
from tests.artifacts.helpers import step
from tests.product_config.conftest import write_config

pytestmark = pytest.mark.skipif(
    os.name not in {"posix", "nt"}, reason="完整产品备份只验证POSIX和Windows原生端口"
)


@pytest.fixture
async def complete_state(tmp_path, config, monkeypatch):
    monkeypatch.setenv("PRIMARY_API_KEY", "backup-primary-fixture")
    monkeypatch.setenv("BACKUP_API_KEY", "backup-secondary-fixture")
    workspace, root = tmp_path / "workspace", tmp_path / "state"
    workspace.mkdir()
    (workspace / "x.txt").write_text("needle safe\n" * 300)

    async def build(snapshot, selection, secrets, *, audit):
        return await build_provider_bundle(
            snapshot,
            selection,
            secrets,
            audit=audit,
            factory=lambda *_: ScriptedProvider([step(), answer()]),
        )

    async def drive(server, _input, _output):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            thread = await client.create_thread(str(workspace), request_id="backup-thread")
            await client.start_turn(thread.thread_id, "搜索并保存产物", request_id="backup-turn")
            async with asyncio.timeout(5):
                while server.service._tasks:
                    await asyncio.gather(*tuple(server.service._tasks.values()))
            assert (await client.get_thread(thread.thread_id)).latest_turn.status == "completed"
        finally:
            await client.close()

    monkeypatch.setattr(product, "build_provider_bundle", build)
    monkeypatch.setattr(product, "run_stdio", drive)
    await product.run_product_stdio(
        config_path=write_config(tmp_path / "config.json", config),
        profile_id=None,
        workspace=workspace,
        state_directory=root,
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )
    prepared = prepare_workspace_transaction(
        workspace,
        {"x.txt": DesiredWorkspaceFile(b"changed\n", 0o644)},
        request_id="backup-transaction",
        now=utc_now(),
    )
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions") as transactions:
        transactions.save(prepared)
    with closing(sqlite3.connect(root / "sessions.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] > 0
    return root, prepared


async def test_complete_authenticated_backup_preserves_all_stores_artifact_and_blobs(
    complete_state,
    tmp_path,
):
    from harnessix.product_config.state_backup import backup_product_state, verify_product_backup

    root, prepared = complete_state
    destination = tmp_path / "backup"
    manifest = await backup_product_state(root, destination)
    checked = await verify_product_backup(root, destination)
    assert checked == manifest
    paths = {entry.path for entry in manifest.files}
    assert {
        "product-config.db",
        "sessions.db",
        "execution-plans.db",
        "action-audit.db",
        "workspace-leases.db",
        "workspace-transactions/transactions.db",
        "session-auth/key.v1",
    } <= paths
    assert not any(path.endswith(("-wal", "-shm", ".lock")) for path in paths)
    for digest, body in prepared.blobs.items():
        assert (
            destination / "state" / "workspace-transactions" / "blobs" / digest
        ).read_bytes() == body
    with sqlite3.connect(destination / "state" / "sessions.db") as target:
        with sqlite3.connect(root / "sessions.db") as source:
            for table in ("agent_events", "agent_event_publications", "agent_artifacts"):
                assert (
                    target.execute("SELECT * FROM " + table).fetchall()
                    == source.execute(
                        "SELECT * FROM " + table,
                    ).fetchall()
                )
    assert (destination / "state" / "session-auth" / "key.v1").read_bytes() == (
        root / "session-auth" / "key.v1"
    ).read_bytes()


@pytest.mark.parametrize("damage", ["blob", "event", "artifact", "schema", "unknown-file"])
async def test_damaged_source_never_publishes_backup(complete_state, tmp_path, damage):
    from harnessix.product_config.state_backup import backup_product_state

    root, prepared = complete_state
    if damage == "blob":
        (root / "workspace-transactions" / "blobs" / next(iter(prepared.blobs))).unlink()
    elif damage == "unknown-file":
        (root / "foreign-data.txt").write_text("not managed")
    else:
        with sqlite3.connect(root / "sessions.db") as db:
            if damage == "event":
                db.execute("UPDATE agent_event_publications SET seal=x'00'")
            elif damage == "artifact":
                db.execute("UPDATE agent_artifacts SET publication_seal=x'00'")
            else:
                db.execute("UPDATE agent_migrations SET checksum='wrong' WHERE version=1")
    with pytest.raises(KernelError):
        await backup_product_state(root, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    assert not list(state_owner_anchor(root).glob("backup-*.json"))


async def test_untrusted_modified_bundle_is_rejected_without_changing_current_state(
    complete_state,
    tmp_path,
):
    from harnessix.product_config.state_backup import backup_product_state, verify_product_backup

    root, _ = complete_state
    destination = tmp_path / "backup"
    await backup_product_state(root, destination)
    before = hashlib.sha256((root / "sessions.db").read_bytes()).hexdigest()
    (destination / "state" / "session-auth" / "key.v1").write_bytes(b"untrusted")
    with pytest.raises(KernelError):
        await verify_product_backup(root, destination)
    assert hashlib.sha256((root / "sessions.db").read_bytes()).hexdigest() == before


async def test_missing_source_root_keeps_independent_backup_trust(complete_state, tmp_path):
    from harnessix.product_config.state_backup import backup_product_state, verify_product_backup

    root, _ = complete_state
    destination = tmp_path / "backup"
    manifest = await backup_product_state(root, destination)
    root.rename(tmp_path / "retained-source")
    assert await verify_product_backup(root, destination) == manifest
    assert not root.exists()


async def test_backup_does_not_overwrite_destination_or_bypass_live_owner(complete_state, tmp_path):
    from harnessix.product_config.state_backup import backup_product_state

    root, _ = complete_state
    destination = tmp_path / "backup"
    destination.mkdir(mode=0o700)
    (destination / "user.txt").write_bytes(b"keep")
    with pytest.raises(KernelError):
        await backup_product_state(root, destination)
    assert (destination / "user.txt").read_bytes() == b"keep"
    with product_state_owner(root):
        with pytest.raises(KernelError, match="产品状态"):
            await backup_product_state(root, tmp_path / "second")


async def test_backup_rejects_busy_database_without_model_or_recovery_effects(
    complete_state, tmp_path
):
    from harnessix.product_config.state_backup import backup_product_state

    root, _ = complete_state
    with sqlite3.connect(root / "execution-plans.db") as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(KernelError):
            await backup_product_state(root, tmp_path / "backup")
        writer.rollback()
    assert not (tmp_path / "backup").exists()


async def test_source_blob_change_after_copy_is_not_published(complete_state, tmp_path):
    from harnessix.product_config.state_backup import backup_product_state

    root, prepared = complete_state
    digest = sorted(prepared.blobs)[0]
    relative = "workspace-transactions/blobs/" + digest

    def fault(phase):
        if phase == "backup.after_copy:" + relative:
            (root / relative).write_bytes(b"changed-after-copy")

    with pytest.raises(KernelError):
        await backup_product_state(root, tmp_path / "backup", fault=fault)
    assert not (tmp_path / "backup").exists()


async def test_repeated_cancel_settles_original_writer_before_owner_release(
    complete_state,
    tmp_path,
):
    from harnessix.product_config.state_backup import backup_product_state

    root, _ = complete_state
    reached, release = threading.Event(), threading.Event()
    calls = []

    def fault(phase):
        if phase == "backup.after_quiet":
            calls.append(phase)
            reached.set()
            assert release.wait(5)

    task = asyncio.create_task(backup_product_state(root, tmp_path / "backup", fault=fault))
    try:
        assert await asyncio.to_thread(reached.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(KernelError) as busy:
            with product_state_owner(root):
                pass
        assert busy.value.code == "product_state_busy"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert len(calls) == 1
    assert not (tmp_path / "backup").exists()
    assert not list(state_owner_anchor(root).glob("backup-*.json"))
    with product_state_owner(root):
        pass


@pytest.mark.parametrize(
    "phase", ["backup.after_quiet", "backup.after_receipt", "backup.after_publish"]
)
async def test_backup_failure_windows_have_unambiguous_publication(complete_state, tmp_path, phase):
    from harnessix.product_config.state_backup import backup_product_state, verify_product_backup

    root, _ = complete_state

    def fault(current):
        if current == phase:
            raise KernelError("injected_backup_failure", "受控故障")

    with pytest.raises(KernelError, match="受控故障"):
        await backup_product_state(root, tmp_path / "backup", fault=fault)
    if phase == "backup.after_publish":
        await verify_product_backup(root, tmp_path / "backup")
    else:
        assert not (tmp_path / "backup").exists()
        assert not list(state_owner_anchor(root).glob("backup-*.json"))


async def test_valid_backup_requires_original_external_receipt(complete_state, tmp_path):
    from harnessix.product_config.state_backup import backup_product_state, verify_product_backup

    root, _ = complete_state
    manifest = await backup_product_state(root, tmp_path / "backup")
    (state_owner_anchor(root) / f"backup-{manifest.backup_id}.json").unlink()
    with pytest.raises(KernelError):
        await verify_product_backup(root, tmp_path / "backup")
    assert not list(state_owner_anchor(root).glob("backup-*.json"))


async def test_wrong_current_key_is_rejected_without_resigning_backup(complete_state, tmp_path):
    from harnessix.product_config.state_backup import backup_product_state, verify_product_backup

    root, _ = complete_state
    await backup_product_state(root, tmp_path / "backup")
    original = (root / "session-auth" / "key.v1").read_bytes()
    (root / "session-auth" / "key.v1").write_bytes(original[:-32] + bytes(range(32)))
    before = (tmp_path / "backup" / "state" / "session-auth" / "key.v1").read_bytes()
    with pytest.raises(KernelError) as error:
        await verify_product_backup(root, tmp_path / "backup")
    assert error.value.code == "product_backup_key_mismatch"
    assert (tmp_path / "backup" / "state" / "session-auth" / "key.v1").read_bytes() == before


async def test_sqlite_backup_includes_committed_wal_without_copying_sidecars(
    complete_state, tmp_path
):
    from uuid import UUID

    from harnessix.agent.models import EventDraft, ThreadArchived
    from harnessix.product_config.session_key import open_product_session_binding
    from harnessix.product_config.state_backup import backup_product_state
    from harnessix.session.sqlite import SQLiteSessionStore
    from tests.agent.test_publication import protected

    root, _ = complete_state
    with sqlite3.connect(root / "sessions.db") as pinned:
        pinned.execute("BEGIN")
        identity, sequence = pinned.execute(
            "SELECT thread_id,sequence FROM agent_threads"
        ).fetchone()
        with protected() as scope:
            async with open_product_session_binding(root, scope) as binding:
                store = SQLiteSessionStore(root / "sessions.db", publication=binding)
                await store.append(
                    UUID(identity),
                    [EventDraft(payload=ThreadArchived(reason=None))],
                    expected_sequence=sequence,
                )
        assert (root / "sessions.db-wal").exists()
        manifest = await backup_product_state(root, tmp_path / "backup")
        with sqlite3.connect(tmp_path / "backup" / "state" / "sessions.db") as copied:
            assert (
                copied.execute("SELECT sequence FROM agent_threads").fetchone()[0] == sequence + 1
            )
        assert not any(entry.path.endswith(("-wal", "-shm")) for entry in manifest.files)
        pinned.rollback()


async def test_readonly_connection_closing_during_sidecar_inventory_is_safe(
    complete_state,
    tmp_path,
    monkeypatch,
):
    from harnessix.product_config.state_backup import backup_product_state
    from harnessix.product_config.state_backup_files import PrivateStateTree

    root, _ = complete_state
    reader = sqlite3.connect(root / "sessions.db", check_same_thread=False)
    reader.execute("SELECT COUNT(*) FROM agent_events").fetchone()
    assert (root / "sessions.db-shm").exists()
    original = PrivateStateTree.open_file
    closed = []

    @contextmanager
    def racing(tree, relative, **kwargs):
        with original(tree, relative, **kwargs) as descriptor:
            yield descriptor
            if tree.path == root and relative == "sessions.db-shm" and not closed:
                reader.close()
                assert not (root / "sessions.db-shm").exists()
                closed.append(True)

    monkeypatch.setattr(PrivateStateTree, "open_file", racing)
    try:
        await backup_product_state(root, tmp_path / "backup")
    finally:
        reader.close()
    assert closed == [True]


async def test_optional_actual_process_receipt_and_output_are_backed_up(complete_state, tmp_path):
    import sys

    from harnessix.execution.store import SQLiteExecutionPlanStore
    from harnessix.processes.supervision_planner import build_process_spec
    from harnessix.processes.supervisor import PosixProcessSupervisor, WindowsProcessSupervisor
    from harnessix.product_config.state_backup import backup_product_state, verify_product_backup

    if os.name == "nt":
        from tests.processes.test_windows_supervisor import _plan
    else:
        from tests.processes.test_supervisor import _plan

    root, _ = complete_state
    workspace = tmp_path / "workspace"
    # 运行实际平台Owner和对应Plan，不用POSIX能力声明替代Windows Job Object事实。
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    async with supervisor_type(root / "process-owner") as supervisor:
        spec = build_process_spec(
            invocation="argv", argv=(sys.executable, "-I", "-c", "print('ok')"), output_bytes=4096
        )
        plan = _plan(workspace, spec, supervisor)
        with SQLiteExecutionPlanStore(root / "execution-plans.db") as plans:
            plans.save_plan(plan)
        handle = await supervisor.start(
            plan, spec, supervisor.capability, workspace=workspace, environment={}
        )
        lease = await handle.wait()
        assert lease.state == "exited"
    manifest = await backup_product_state(root, tmp_path / "backup")
    assert await verify_product_backup(root, tmp_path / "backup") == manifest
    paths = {entry.path for entry in manifest.files}
    assert "process-owner/process-leases.db" in paths
    assert {
        f"process-owner/runs/{spec.process_id}/{name}"
        for name in (
            "receipt.json",
            "stdout.bin",
            "stderr.bin",
        )
    } <= paths


@pytest.mark.parametrize("budget", [0.0, -1.0, float("inf"), float("nan"), 301.0])
async def test_invalid_budget_is_rejected_before_any_state_creation(tmp_path, budget):
    from harnessix.product_config.state_backup import backup_product_state

    with pytest.raises(KernelError) as error:
        await backup_product_state(tmp_path / "state", tmp_path / "backup", budget_seconds=budget)
    assert error.value.code == "product_backup_budget_invalid"
    assert not (tmp_path / "state").exists()
    assert not state_owner_anchor(tmp_path / "state").exists()


async def test_cli_backup_and_verify_use_real_root_without_secret_output(
    complete_state,
    tmp_path,
    capsys,
):
    import json

    from harnessix.cli import main

    root, _ = complete_state
    args = ["--state-directory", str(root), "--backup-directory", str(tmp_path / "backup")]
    await asyncio.to_thread(main, ["state", "backup", *args])
    backed_up = json.loads(capsys.readouterr().out)
    await asyncio.to_thread(main, ["state", "verify", *args])
    verified = json.loads(capsys.readouterr().out)
    assert backed_up["status"] == "backed_up" and verified["status"] == "verified"
    assert backed_up["backup_id"] == verified["backup_id"]
    assert set(backed_up) == {"status", "backup_id", "files", "size_bytes"}


async def test_backup_never_writes_original_key_into_managed_workspace(complete_state, tmp_path):
    from harnessix.product_config.state_backup import backup_product_state

    root, _ = complete_state
    with pytest.raises(KernelError) as error:
        await backup_product_state(root, tmp_path / "workspace" / "backup")
    assert error.value.code == "product_backup_workspace_overlap"
    assert not (tmp_path / "workspace" / "backup").exists()
    assert not list((tmp_path / "workspace").glob(".backup.backup-*"))


@pytest.mark.parametrize("store", ["config", "execution", "audit", "delivery", "process"])
def test_readonly_store_never_creates_or_registers_missing_database(tmp_path, store):
    from harnessix.execution.store import SQLiteExecutionPlanStore
    from harnessix.processes.supervision_store import SQLiteProcessLeaseStore
    from harnessix.product_config.action_store import SQLiteProductRuntimeConfigStore
    from harnessix.trusted_actions.store import SQLiteActionAuditStore

    root = tmp_path / "missing"
    factories = {
        "config": lambda: SQLiteProductRuntimeConfigStore(root / "config.db", read_only=True),
        "execution": lambda: SQLiteExecutionPlanStore(root / "plans.db", read_only=True),
        "audit": lambda: SQLiteActionAuditStore(root / "audit.db", read_only=True),
        "delivery": lambda: SQLiteWorkspaceTransactionStore(root, read_only=True),
        "process": lambda: SQLiteProcessLeaseStore(root / "leases.db", read_only=True),
    }
    with pytest.raises(sqlite3.OperationalError):
        factories[store]()
    assert not root.exists()


async def test_receipt_collision_preserves_original_trust_record(
    complete_state, tmp_path, monkeypatch
):
    from uuid import UUID

    from harnessix.product_config import state_backup

    root, _ = complete_state
    monkeypatch.setattr(state_backup, "uuid4", lambda: UUID(int=19))
    first = await state_backup.backup_product_state(root, tmp_path / "first")
    record = state_owner_anchor(root) / f"backup-{first.backup_id}.json"
    before = record.read_bytes()
    with pytest.raises(KernelError):
        await state_backup.backup_product_state(root, tmp_path / "second")
    assert record.read_bytes() == before
    assert await state_backup.verify_product_backup(root, tmp_path / "first") == first


async def test_timeout_cleans_own_candidate_and_releases_original_owner(complete_state, tmp_path):
    import time

    from harnessix.product_config.state_backup import backup_product_state

    root, _ = complete_state

    def fault(phase):
        if phase == "backup.after_quiet":
            time.sleep(0.2)

    with pytest.raises(KernelError) as error:
        await backup_product_state(root, tmp_path / "backup", budget_seconds=0.1, fault=fault)
    assert error.value.code == "maintenance_io_timeout"
    assert not (tmp_path / "backup").exists()
    assert not list(state_owner_anchor(root).glob("backup-*.json"))
    with product_state_owner(root):
        pass


async def test_publication_confirmation_loss_preserves_published_backup_and_receipt(
    complete_state, tmp_path, monkeypatch
):
    from harnessix.product_config import state_backup

    root, _ = complete_state
    publish = state_backup.publish_tree

    def publish_then_lose_confirmation(source, target):
        publish(source, target)
        raise OSError("publication confirmation unavailable")

    monkeypatch.setattr(state_backup, "publish_tree", publish_then_lose_confirmation)
    with pytest.raises(KernelError):
        await state_backup.backup_product_state(root, tmp_path / "backup")
    assert (tmp_path / "backup").is_dir()
    manifest = await state_backup.verify_product_backup(root, tmp_path / "backup")
    assert (state_owner_anchor(root) / f"backup-{manifest.backup_id}.json").is_file()
    assert not list(tmp_path.glob(".backup.backup-*"))
