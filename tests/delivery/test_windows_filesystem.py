from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery import filesystem, windows_filesystem
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action import workspace_patch_supported
from harnessix.delivery.windows_filesystem import _parent
from harnessix.delivery.windows_io import WindowsFileOperations, _rename_buffer
from harnessix.workspace.leases import WorkspaceLeaseStore
from harnessix.workspace.windows import _last_error

pytestmark = pytest.mark.skipif(os.name != "nt", reason="原生Windows NTFS句柄写入与故障恢复")

_CRASH_WORKER = """
import os, sys
from pathlib import Path
from uuid import UUID
from harnessix.delivery import filesystem
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.leases import WorkspaceLeaseStore
state, workspace, transaction_id, point = sys.argv[1:]
def crash(current):
    if current == point:
        os._exit(87)
filesystem._fault = crash
with SQLiteWorkspaceTransactionStore(Path(state) / 'transactions') as store:
    with WorkspaceLeaseStore(Path(state) / 'leases.db') as leases:
        runtime = WorkspaceTransactionRuntime(store, leases)
        record = store.load(UUID(transaction_id))
        lease = leases.acquire(record.plan.source.workspace_id, 'crash-worker', ttl_seconds=60)
        runtime.publish(record.transaction_id, Path(workspace),
            approval_fingerprint=record.plan.fingerprint, lease=lease)
raise AssertionError('未到达原生硬退出切点')
"""


def test_native_rename_api_compatibility_matrix(tmp_path: Path) -> None:
    results = {}
    with _parent(tmp_path, "probe.py") as (native, operations, parent, directory, _):
        for kind in ("relative", "relative-nul", "absolute-nul", "native-same-parent"):
            path = directory / f"{kind}.tmp"
            handle = operations.create_temporary(path)
            try:
                operations.write_and_flush(handle, b"probe")
                name = f"{kind}.py"
                if kind == "native-same-parent":
                    try:
                        operations.rename(handle, name, replace=False)
                        results[kind] = {"accepted": True, "errno": 0}
                    except OSError as error:
                        results[kind] = {"accepted": False, "errno": error.errno}
                    continue
                root = parent
                if kind == "absolute-nul":
                    root = 0
                    name = native._final_path(parent) + "\\" + name
                original = _rename_buffer(root, name, replace=False)
                buffer = original
                if kind.endswith("nul"):
                    buffer = ctypes.create_string_buffer(len(original) + 8)
                    ctypes.memmove(buffer, original, len(original))
                accepted = bool(
                    operations.kernel.SetFileInformationByHandle(handle, 22, buffer, len(buffer))
                )
                results[kind] = {"accepted": accepted, "errno": 0 if accepted else _last_error()}
            finally:
                operations.kernel.CloseHandle(handle)
    assert results["native-same-parent"]["accepted"], results


@pytest.fixture
def state(tmp_path: Path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state/transactions") as store:
        with WorkspaceLeaseStore(tmp_path / "state/leases.db") as leases:
            yield store, leases, WorkspaceTransactionRuntime(store, leases)


def _publish(state, workspace: Path, desired: dict[str, DesiredWorkspaceFile]):
    store, leases, runtime = state
    record = store.save(prepare_workspace_transaction(workspace, desired, request_id="native"))
    lease = leases.acquire(record.plan.source.workspace_id, "native-owner", ttl_seconds=60)
    try:
        return runtime.publish(
            record.transaction_id,
            workspace,
            approval_fingerprint=record.plan.fingerprint,
            lease=lease,
        )
    except KernelError as error:
        cause = error.__context__
        if isinstance(cause, OSError):
            pytest.fail(f"native IO failure: code={error.code}; win32_errno={cause.errno}")
        raise


def test_native_create_replace_delete_rollback_and_idempotent_result(tmp_path: Path, state) -> None:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src/existing.py").write_bytes(b"before\n")
    (root / "delete.txt").write_bytes(b"restore\n")
    assert workspace_patch_supported(root)
    record = _publish(
        state,
        root,
        {
            "src/existing.py": DesiredWorkspaceFile(b"after\n", 0o644),
            "src/新增 😀.py": DesiredWorkspaceFile(b"new\n", 0o644),
            "delete.txt": DesiredWorkspaceFile(None),
        },
    )
    assert record.state == "published" and record.cursor == 3
    assert (root / "src/existing.py").read_bytes() == b"after\n"
    assert (root / "src/新增 😀.py").read_bytes() == b"new\n"
    assert not (root / "delete.txt").exists() and not list(root.rglob(".harnessix-*.tmp"))
    store, leases, runtime = state
    lease = leases.acquire(record.plan.source.workspace_id, "native-owner", ttl_seconds=60)
    assert (
        runtime.publish(
            record.transaction_id, root, approval_fingerprint=record.plan.fingerprint, lease=lease
        )
        == record
    )
    rollback = runtime.build_rollback(record.transaction_id, root, request_id="native-rollback")
    restored = runtime.publish(
        rollback.transaction_id, root, approval_fingerprint=rollback.plan.fingerprint, lease=lease
    )
    assert restored.state == "published" and rollback.transaction_id != record.transaction_id
    assert store.load(record.transaction_id) == record
    assert (root / "src/existing.py").read_bytes() == b"before\n"
    assert (root / "delete.txt").read_bytes() == b"restore\n"
    assert not (root / "src/新增 😀.py").exists()


@pytest.mark.parametrize("body", [b"", b"\x00\xff" * 32_771], ids=["empty", "multiple-chunks"])
def test_empty_and_multi_chunk_binary_files_have_exact_bytes(tmp_path: Path, state, body: bytes):
    record = _publish(state, tmp_path, {"binary.dat": DesiredWorkspaceFile(body, 0o644)})
    assert record.state == "published" and (tmp_path / "binary.dat").read_bytes() == body


def test_unicode_spaces_long_path_uses_same_pinned_parent(tmp_path: Path, state) -> None:
    root = tmp_path / "项目 workspace"
    relative = "/".join(["长路径segment" * 6] * 4) + "/主程序 😀.py"
    target = root / Path(*relative.split("/"))
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before")
    assert len(str(target)) > 260
    record = _publish(state, root, {relative: DesiredWorkspaceFile("新内容".encode(), 0o644)})
    assert record.state == "published" and target.read_bytes() == "新内容".encode()


def test_source_drift_and_approval_fencing_leave_before_untouched(tmp_path: Path, state) -> None:
    target = tmp_path / "target.py"
    target.write_bytes(b"before")
    store, leases, runtime = state
    record = store.save(
        prepare_workspace_transaction(
            tmp_path, {"target.py": DesiredWorkspaceFile(b"after", 0o644)}, request_id="guard"
        )
    )
    lease = leases.acquire(record.plan.source.workspace_id, "native-owner", ttl_seconds=60)
    with pytest.raises(KernelError) as approval:
        runtime.publish(record.transaction_id, tmp_path, approval_fingerprint="f" * 64, lease=lease)
    assert approval.value.code == "delivery_approval_mismatch"
    with pytest.raises(KernelError) as fencing:
        runtime.publish(
            record.transaction_id,
            tmp_path,
            approval_fingerprint=record.plan.fingerprint,
            lease=lease.model_copy(update={"workspace_id": "f" * 64}),
        )
    assert fencing.value.code == "workspace_lease_lost"
    target.write_bytes(b"changed-by-user")
    with pytest.raises(KernelError) as changed:
        runtime.publish(
            record.transaction_id,
            tmp_path,
            approval_fingerprint=record.plan.fingerprint,
            lease=lease,
        )
    assert changed.value.code == "execution_plan_stale"
    assert store.load(record.transaction_id).state == "prepared"
    assert target.read_bytes() == b"changed-by-user"


def test_empty_parent_chain_is_pinned_before_any_temporary_creation(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    parent = root / "src"
    parent.mkdir(parents=True)
    with _parent(root, "src/new.py"):
        with pytest.raises(OSError):
            parent.rename(root / "moved")
        with pytest.raises(OSError):
            root.rename(tmp_path / "moved-workspace")
    assert parent.is_dir() and not list(parent.iterdir())


def test_replacement_target_renamed_during_flush_is_rejected_without_overwriting_foreign_file(
    tmp_path: Path, state, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.py"
    target.write_bytes(b"before")

    def retarget(point: str):
        if point == "windows_temporary_flushed":
            # 使用同目录原生句柄模拟名称漂移；路径Rename自身会受到父链共享约束。
            with _parent(tmp_path, "target.py") as (_, operations, _, _, _):
                handle = operations.open_existing(target, delete=True)
                assert handle is not None
                try:
                    operations.rename(handle, "original.py", replace=False)
                finally:
                    operations.kernel.CloseHandle(handle)
            target.write_bytes(b"foreign")

    monkeypatch.setattr(filesystem, "_fault", retarget)
    with pytest.raises(KernelError) as changed:
        _publish(state, tmp_path, {"target.py": DesiredWorkspaceFile(b"after", 0o644)})
    assert changed.value.code == "delivery_source_changed"
    assert target.read_bytes() == b"foreign"
    assert (tmp_path / "original.py").read_bytes() == b"before"
    assert not list(tmp_path.glob(".harnessix-*.tmp"))


@pytest.mark.parametrize("kind", ["readonly", "ads", "hardlink", "hidden"])
def test_special_source_metadata_is_not_discarded(tmp_path: Path, state, kind: str) -> None:
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    if kind == "readonly":
        target.chmod(0o444)
    elif kind == "ads":
        Path(str(target) + ":Zone.Identifier").write_bytes(b"keep-ads")
    elif kind == "hardlink":
        os.link(target, tmp_path / "second-link.txt")
    else:
        subprocess.run(["attrib", "+H", str(target)], check=True, capture_output=True)
    try:
        with pytest.raises(KernelError) as denied:
            _publish(state, tmp_path, {"target.txt": DesiredWorkspaceFile(b"after", 0o644)})
        assert denied.value.code in {"delivery_metadata_unsupported", "workspace_path_denied"}
        assert target.read_bytes() == b"before"
        if kind == "ads":
            assert Path(str(target) + ":Zone.Identifier").read_bytes() == b"keep-ads"
        assert not list(tmp_path.glob(".harnessix-*.tmp"))
    finally:
        target.chmod(0o666)
        if kind == "hidden":
            subprocess.run(["attrib", "-H", str(target)], check=True, capture_output=True)


def test_custom_security_is_rejected_without_broadening_acl(tmp_path: Path, state) -> None:
    target = tmp_path / "target.py"
    target.write_bytes(b"before")
    subprocess.run(["icacls", str(target), "/inheritance:d"], check=True, capture_output=True)
    with pytest.raises(KernelError) as denied:
        _publish(state, tmp_path, {"target.py": DesiredWorkspaceFile(b"after", 0o644)})
    assert denied.value.code == "delivery_metadata_unsupported"
    assert target.read_bytes() == b"before" and not list(tmp_path.glob(".harnessix-*.tmp"))


def test_junction_parent_is_rejected_without_touching_outside(tmp_path: Path, state) -> None:
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "target.py").write_bytes(b"outside")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "src"), str(outside)],
        check=True,
        capture_output=True,
    )
    try:
        with pytest.raises(KernelError) as denied:
            _publish(state, root, {"src/target.py": DesiredWorkspaceFile(b"after", 0o644)})
        assert denied.value.code == "workspace_path_denied"
        assert (outside / "target.py").read_bytes() == b"outside"
    finally:
        (root / "src").rmdir()


def test_pinned_parent_leaf_and_temporary_refuse_concurrent_rename_or_write(
    tmp_path: Path, state, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "src"
    parent.mkdir()
    target = parent / "target.py"
    target.write_bytes(b"before")
    checked = []

    def check(point: str):
        if point == "windows_temporary_created":
            with pytest.raises(OSError):
                parent.rename(tmp_path / "moved")
            with pytest.raises(OSError):
                target.write_bytes(b"foreign-write")
            temporary = next(parent.glob(".harnessix-*.tmp"))
            with pytest.raises(OSError):
                temporary.write_bytes(b"foreign-temp-write")
            checked.append(point)

    monkeypatch.setattr(filesystem, "_fault", check)
    record = _publish(state, tmp_path, {"src/target.py": DesiredWorkspaceFile(b"after", 0o644)})
    assert record.state == "published" and checked == ["windows_temporary_created"]
    assert target.read_bytes() == b"after"


def test_foreign_target_appearing_before_create_is_never_overwritten(
    tmp_path: Path, state, monkeypatch: pytest.MonkeyPatch
) -> None:
    def foreign(point: str):
        if point == "windows_temporary_flushed":
            (tmp_path / "target.py").write_bytes(b"foreign")

    monkeypatch.setattr(filesystem, "_fault", foreign)
    with pytest.raises(KernelError) as changed:
        _publish(state, tmp_path, {"target.py": DesiredWorkspaceFile(b"after", 0o644)})
    assert changed.value.code == "delivery_source_changed"
    assert (tmp_path / "target.py").read_bytes() == b"foreign"
    assert not list(tmp_path.glob(".harnessix-*.tmp"))


def test_native_rename_confirmation_lost_reconciles_once_without_deleting_published_file(
    tmp_path: Path, state, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "target.py").write_bytes(b"before")
    original = WindowsFileOperations.rename
    calls = []

    def lost(self, handle, name, *, replace):
        original(self, handle, name, replace=replace)
        calls.append(name)
        raise OSError("原生Rename已提交但确认丢失")

    monkeypatch.setattr(WindowsFileOperations, "rename", lost)
    monkeypatch.setattr(
        windows_filesystem,
        "_discard_owned_temporary",
        lambda *_: pytest.fail("Rename请求后不得清理"),
    )
    record = _publish(state, tmp_path, {"target.py": DesiredWorkspaceFile(b"after", 0o644)})
    assert record.state == "published" and calls == ["target.py"]
    assert (tmp_path / "target.py").read_bytes() == b"after"
    assert not list(tmp_path.glob(".harnessix-*.tmp"))


def test_replaced_workspace_root_never_accepts_copied_member_facts(
    tmp_path: Path, state, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_bytes(b"a-before")
    (root / "b.py").write_bytes(b"b-before")

    def replace_root(point: str):
        if point == "member_recorded:0":
            root.rename(tmp_path / "original-workspace")
            root.mkdir()
            (root / "a.py").write_bytes(b"a-after")
            (root / "b.py").write_bytes(b"b-before")

    monkeypatch.setattr(filesystem, "_fault", replace_root)
    with pytest.raises(KernelError) as changed:
        _publish(
            state,
            root,
            {
                "a.py": DesiredWorkspaceFile(b"a-after", 0o644),
                "b.py": DesiredWorkspaceFile(b"b-after", 0o644),
            },
        )
    assert changed.value.code == "delivery_source_changed"
    assert (root / "b.py").read_bytes() == b"b-before"
    assert (tmp_path / "original-workspace/b.py").read_bytes() == b"b-before"


def test_expired_lease_stops_before_next_native_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    clock = [100.0]
    with SQLiteWorkspaceTransactionStore(tmp_path / "state/transactions") as store:
        with WorkspaceLeaseStore(tmp_path / "state/leases.db", clock=lambda: clock[0]) as leases:
            record = store.save(
                prepare_workspace_transaction(
                    root,
                    {
                        "a.py": DesiredWorkspaceFile(b"a-after", 0o644),
                        "b.py": DesiredWorkspaceFile(b"b-after", 0o644),
                    },
                    request_id="expired-lease",
                )
            )
            lease = leases.acquire(record.plan.source.workspace_id, "native-owner", ttl_seconds=1)

            def expire(point: str):
                if point == "member_recorded:0":
                    clock[0] = 102.0

            monkeypatch.setattr(filesystem, "_fault", expire)
            with pytest.raises(KernelError) as lost:
                WorkspaceTransactionRuntime(store, leases).publish(
                    record.transaction_id,
                    root,
                    approval_fingerprint=record.plan.fingerprint,
                    lease=lease,
                )
            assert lost.value.code == "workspace_lease_lost"
            assert store.load(record.transaction_id).cursor == 1
    assert (root / "a.py").read_bytes() == b"a-after" and not (root / "b.py").exists()


@pytest.mark.parametrize(
    "point, cursor",
    [
        ("windows_temporary_flushed", 0),
        ("windows_namespace_changed", 1),
        ("effect_applied:0", 1),
        ("member_recorded:0", 1),
    ],
)
def test_real_exit_reopen_only_observes_then_explicitly_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str, cursor: int
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_bytes(b"a-before")
    (root / "b.py").write_bytes(b"b-before")
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state / "transactions") as store:
        record = store.save(
            prepare_workspace_transaction(
                root,
                {
                    "a.py": DesiredWorkspaceFile(b"a-after", 0o644),
                    "b.py": DesiredWorkspaceFile(b"b-after", 0o644),
                },
                request_id="native-crash",
            )
        )
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            _CRASH_WORKER,
            str(state),
            str(root),
            str(record.transaction_id),
            point,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 87, process.stderr
    before_a, before_b = (root / "a.py").read_bytes(), (root / "b.py").read_bytes()
    original_apply = filesystem._apply
    monkeypatch.setattr(filesystem, "_apply", lambda *_: pytest.fail("只观察恢复不得写入"))
    with SQLiteWorkspaceTransactionStore(state / "transactions") as store:
        with WorkspaceLeaseStore(state / "leases.db") as leases:
            runtime = WorkspaceTransactionRuntime(store, leases)
            interrupted = runtime.reconcile(record.transaction_id, root)
            assert interrupted.state == "interrupted" and interrupted.cursor == cursor
            assert (root / "a.py").read_bytes() == before_a
            assert (root / "b.py").read_bytes() == before_b == b"b-before"
            monkeypatch.setattr(filesystem, "_apply", original_apply)
            lease = leases.acquire(record.plan.source.workspace_id, "crash-worker", ttl_seconds=60)
            published = runtime.publish(
                record.transaction_id,
                root,
                approval_fingerprint=record.plan.fingerprint,
                lease=lease,
            )
    assert published.state == "published"
    assert (root / "a.py").read_bytes() == b"a-after" and (root / "b.py").read_bytes() == b"b-after"
    if cursor == 0:
        assert len(list(root.glob(".harnessix-*.tmp"))) == 1  # 硬退出残留不由泛路径GC清理。
