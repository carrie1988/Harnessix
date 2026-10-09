"""User collector 只读进度机制；权限/Session 替身不计为业务或 SDK 验收。

真实 _collect 托管子 Task、Source 的原事实观察、Snapshot codec 和耐久 CAS 均执行；
在真实观察完成后主动停止，不伪造用户 Git 基准或准备成功。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread
from harnessix.delivery.contracts import WorkspaceFileVersion, WorkspaceMutation
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import git_checkpoint_preparation as preparation
from harnessix.product_config import git_delivery_source as source
from harnessix.product_config import git_user_observation as user
from harnessix.product_config import git_user_observation_contracts as contracts
from harnessix.product_config.git_baseline_contracts import (
    GitBaselineMember,
    product_git_baseline_digest,
)
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_plan_contracts import GitIndexFileObservation
from harnessix.product_config.git_delivery_process import GitDeliveryProcess, GitOperationBudget
from harnessix.product_config.git_parent_contracts import (
    ProductGitDeliveryBaselineV2,
    ProductGitDeliverySourceV2,
)
from harnessix.product_config.git_user_observation_paths import (
    PinnedGitUserDirectories,
    git_user_directory_facts,
)
from harnessix.product_config.workspace_patch_source_contracts import (
    WorkspacePatchSourceReference,
    product_git_delivery_source_digest,
)
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.tools.git import GitReadRuntime
from harnessix.trusted_actions.router import ActionPlanningContext
from harnessix.workspace import snapshot as native
from harnessix.workspace import snapshot_v2 as snapshots
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts

pytestmark = pytest.mark.skipif(os.name != "posix", reason="真实 POSIX 原生事实和耐久 CAS")

_FROZEN_SHA256 = "e842f98fabf143aa0c86d4dcd6343705c9d9a57940a8b27babcdce89da4da6f0"
_SOURCE_SHA256 = "7b62db51e4830038074ffbe5b86674812a0449f640779c6d0bcde46fc26dda6f"
_ERROR_NAMES = ("os", "kernel", "timeout", "cancel", "task", "marked", "nested")


def _frozen_functions():
    path = Path(__file__).with_name("fixtures") / "git_user_collect_before.txt"
    body = path.read_bytes()
    assert hashlib.sha256(body).hexdigest() == _FROZEN_SHA256
    assert f"# Source SHA256: {_SOURCE_SHA256}".encode() in body
    namespace = dict(vars(user))
    namespace.update(
        product_git_user_observation_fingerprint=contracts.product_git_user_observation_fingerprint,
        git_user_directory_facts=git_user_directory_facts,
    )
    exec(compile(body, str(path), "exec"), namespace)
    return namespace


def _error(name):
    if name == "kernel":
        return KernelError("git_user_observation_unavailable", "original same-code control")
    if name == "marked":
        return UpstreamCheckpointError(OSError("original mark"))
    if name == "nested":
        return UpstreamCheckpointError(UpstreamCheckpointError(OSError("original nested mark")))
    return {
        "os": OSError,
        "timeout": TimeoutError,
        "cancel": TurnCancelled,
        "task": asyncio.CancelledError,
    }[name]("original control")


class _Probe:
    def __init__(self):
        self.phase = "outside"
        self.events = []
        self.full_hook = None
        self.observer_hook = None
        self.saved = []
        self.observed = None
        self.owner = None
        self.parent = None
        self.history_control = None
        self.source_control = None
        self.file_path = None
        self.file_checkpoints = []
        self.file_io_hook = None
        self.child = None
        self.readonly = False
        self.deadline = None
        self.stop = RuntimeError("stop after real Source snapshot, not business success")

    def record(self, mode, detail=None):
        self.events.append((mode, self.phase, detail))

    def full(self):
        previous = self.phase
        if self.file_path is not None:
            self.phase = "file"
        try:
            self.record("full")
            if self.full_hook is not None:
                self.full_hook()
        finally:
            self.phase = previous

    def observer(self):
        if self.file_path is not None and self.phase == "file-source":
            # _segments_check 在 _read_existing 返回后还有一次原局部出口检查。
            self.file_progress(self._observe)
        else:
            self._observe()

    def _observe(self):
        self.record("observer")
        if self.observer_hook is not None:
            self.observer_hook()

    def file_progress(self, checkpoint):
        previous, self.phase = self.phase, "file-progress"
        self.record("file-checkpoint-enter", self.file_path)
        try:
            checkpoint()
        finally:
            self.record("file-checkpoint-exit", self.file_path)
            self.phase = previous

    def forbidden_parent_local(self):
        pytest.fail("collector 不得借任意父控制的 local_check")


@pytest.fixture
def mechanism(tmp_path, monkeypatch):
    root, state = tmp_path / "workspace", tmp_path / "cas"
    requests, versions = [], {}
    for index in range(4):
        path = f"d{index}/deep/正文.txt"
        leaf = root / path
        leaf.parent.mkdir(parents=True)
        leaf.write_bytes(f"native {index}\n".encode())
        requests.append(WorkspaceResourceRequest(path=path, access="read"))
        body, mode = source._read_existing(root, path, "posix")
        versions[path] = (
            WorkspaceFileVersion(presence="absent", size=0),
            WorkspaceFileVersion(
                presence="file", sha256=hashlib.sha256(body).hexdigest(), size=len(body), mode=mode
            ),
        )
    probe = _Probe()
    with SQLiteWorkspaceTransactionStore(state) as store:
        base = snapshots.capture_workspace_snapshot_v2(
            root,
            resources=requests,
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
        )
        store._checkpoint = lambda: probe.record("store-full")
        reader = GitReadRuntime.__new__(GitReadRuntime)
        reader._root = root
        now = datetime.now(UTC)
        thread = Thread(thread_id=uuid4(), workspace=str(root), created_at=now, updated_at=now)
        audit = SimpleNamespace(_checkpoint=lambda: None, _read_blob=store._read_blob)
        router = SimpleNamespace(_audit=audit)
        cancel, budget = CancelToken(), GitOperationBudget(60)
        owner_token = object()

        async def initial_history(thread_id, *, cancel, deadline, checkpoint):
            # 只模拟初始 SessionReader 的合法值，不模拟 MAC/SQL 授权成功。
            assert thread_id == thread.thread_id
            assert deadline <= budget._deadline
            probe.child = asyncio.current_task()
            probe.history_control, probe.deadline = checkpoint, deadline
            probe.record("history")
            checkpoint()
            return AuthenticatedThreadHistory(thread, ())

        session = SimpleNamespace(
            authenticated_thread_history=initial_history, _runtime_owner_token=owner_token
        )
        ports = WorkspaceSnapshotPorts(store.put_blob, store._read_blob)
        case = SimpleNamespace(
            root=root,
            state=state,
            store=store,
            base=base,
            versions=versions,
            thread=thread,
            router=router,
            audit=audit,
            session=session,
            reader=reader,
            cancel=cancel,
            budget=budget,
            ports=ports,
            probe=probe,
            owner_token=owner_token,
        )

        def authority(actual_session, actual_router, actual_store, actual_ports, actual_reader):
            assert all(
                actual is expected
                for actual, expected in zip(
                    (actual_session, actual_router, actual_store, actual_ports, actual_reader),
                    (case.session, case.router, store, case.ports, reader),
                    strict=True,
                )
            )
            probe.record("authority")
            return lambda: probe.record("host")

        def source_boundary(
            actual_thread, targets, actual_router, actual_store, *, checkpoint, snapshot_ports
        ):
            assert (
                actual_thread is thread and actual_router is case.router and actual_store is store
            )
            assert snapshot_ports is case.ports and targets == (thread.thread_id,)
            assert asyncio.current_task() is probe.child and probe.child is not probe.owner
            probe.source_control = checkpoint
            if probe.readonly:
                assert type(probe.history_control) is GitAuthenticationControl
                assert type(checkpoint) is GitAuthenticationControl
                assert checkpoint._origin[2] is probe.child
                assert probe.history_control._origin[2] is probe.child
                assert checkpoint is not probe.parent and probe.history_control is not probe.parent
            else:
                assert type(checkpoint) is not GitAuthenticationControl
                assert type(probe.history_control) is not GitAuthenticationControl
            probe.record("source")
            probe.observed = source._observe_final_versions(
                root, base, versions, checkpoint, case.ports
            )
            raise probe.stop

        # 这是权限机制替身；不计为原真实 Source/Patch 授权、业务或 SDK 通过。
        monkeypatch.setattr(user, "require_git_user_authority", authority)
        monkeypatch.setattr(user, "collect_git_delivery_source", source_boundary)
        _observe_real_io(monkeypatch, case)
        yield case


def _observe_real_io(monkeypatch, case):
    probe = case.probe
    original_capture = snapshots.capture_snapshot_facts
    original_encode = snapshots._encode_snapshot
    original_history = snapshots.read_workspace_parent_closure
    original_snapshot = source.capture_workspace_snapshot_v2
    original_file = source._read_existing
    original_source_file = source._read_source_file
    original_observe = native._PosixRoot.observe
    original_read = case.store._read_blob
    original_write = case.ports.write_blob

    def capture(*args, **kwargs):
        previous, probe.phase = probe.phase, "native"
        probe.saved.append(kwargs["checkpoint"])
        probe.record("capture")
        try:
            return original_capture(*args, **kwargs)
        finally:
            probe.phase = "native-exit" if previous == "capture-entry" else previous

    def encode(*args, **kwargs):
        previous, probe.phase = probe.phase, "encode"
        probe.record("encode")
        try:
            return original_encode(*args, **kwargs)
        finally:
            probe.phase = "encoded" if previous == "native-exit" else previous

    def history(*args, **kwargs):
        previous, probe.phase = probe.phase, "history-cas"
        try:
            return original_history(*args, **kwargs)
        finally:
            probe.phase = previous

    def snapshot(*args, **kwargs):
        previous, probe.phase = probe.phase, "capture-entry"
        try:
            return original_snapshot(*args, **kwargs)
        finally:
            probe.phase = previous

    def observe(root, path, *, access, checkpoint=None):
        probe.record("native-read", path)
        return original_observe(root, path, access=access, checkpoint=checkpoint)

    def file(*args, **kwargs):
        previous, probe.phase = probe.phase, "file"
        probe.record("file-read", args[1])
        checkpoint = kwargs.get("checkpoint")
        if checkpoint is not None:
            probe.file_checkpoints.append(checkpoint)

            def progress():
                # 旧 file 宽段包含原生端口的无 I/O checkpoint；只隔离这次原委托。
                probe.file_progress(checkpoint)

            kwargs = {**kwargs, "checkpoint": progress}
        try:
            return original_file(*args, **kwargs)
        finally:
            probe.phase = previous

    def source_file(root, path, platform, checkpoint):
        # _read_existing 外的原段首末 full 也属于文件边界，不能漏测。
        previous, probe.phase = probe.phase, "file"
        previous_path, probe.file_path = probe.file_path, path
        probe.record("file-source-enter", path)
        probe.phase = "file-source"
        try:
            return original_source_file(root, path, platform, checkpoint)
        finally:
            probe.phase = "file"
            probe.record("file-source-exit", path)
            probe.phase, probe.file_path = previous, previous_path

    def physical_file_io(operation):
        original = getattr(os, operation)

        def io(*args, **kwargs):
            if probe.file_path is None:
                return original(*args, **kwargs)
            # 即使 checkpoint/observer 内意外做 I/O，也必须重新落到 file 负控。
            previous, probe.phase = probe.phase, "file"
            detail = probe.file_path, operation
            probe.record("file-io-enter", detail)
            try:
                if probe.file_io_hook is not None:
                    probe.file_io_hook(operation, "before")
                result = original(*args, **kwargs)
                if probe.file_io_hook is not None:
                    probe.file_io_hook(operation, "after")
                return result
            finally:
                probe.record("file-io-exit", detail)
                probe.phase = previous

        return io

    def physical_read(digest):
        previous, probe.phase = probe.phase, "physical-cas"
        probe.record("physical-read", digest)
        try:
            return original_read(digest)
        finally:
            probe.phase = previous

    def write(digest, body):
        previous, probe.phase = probe.phase, "cas-write"
        probe.record("write", digest)
        try:
            return original_write(digest, body)
        finally:
            probe.phase = previous

    def read(digest):
        probe.record("read", digest)
        return case.store._read_blob(digest)

    monkeypatch.setattr(snapshots, "capture_snapshot_facts", capture)
    monkeypatch.setattr(snapshots, "_encode_snapshot", encode)
    monkeypatch.setattr(snapshots, "read_workspace_parent_closure", history)
    monkeypatch.setattr(source, "capture_workspace_snapshot_v2", snapshot)
    monkeypatch.setattr(source, "_read_existing", file)
    monkeypatch.setattr(source, "_read_source_file", source_file)
    for operation in ("open", "stat", "lstat", "readlink", "fstat", "read", "close"):
        monkeypatch.setattr(os, operation, physical_file_io(operation))
    monkeypatch.setattr(native._PosixRoot, "observe", observe)
    monkeypatch.setattr(case.store, "_read_blob", physical_read)
    # 固定 audit 原 read 引用与实际操作端口，均不修改生产装配。
    case.audit._read_blob = physical_read
    case.ports = WorkspaceSnapshotPorts(write, read)


async def _invoke(case, *, implementation=None, callback=None, observer=None, explicit_none=False):
    probe = case.probe
    probe.owner = asyncio.current_task()
    if callback is None:
        callback = GitAuthenticationControl(probe.forbidden_parent_local, probe.full)
    probe.parent = callback
    probe.readonly = observer is not None
    options = {"native_observer": observer} if observer is not None or explicit_none else {}
    implementation = implementation or user.collect_product_git_user_observation
    return await implementation(
        case.thread,
        (case.thread.thread_id,),
        case.router,
        case.store,
        case.reader,
        session=case.session,
        cancel=case.cancel,
        budget=case.budget,
        checkpoint=callback,
        snapshot_ports=case.ports,
        **options,
    )


def _callback(probe, kind):
    class Proxy:
        def __call__(self):
            probe.full()

    class Subclass(GitAuthenticationControl):
        pass

    if kind == "function":
        return probe.full
    if kind == "proxy":
        return Proxy()
    cls = Subclass if kind == "subclass" else GitAuthenticationControl
    return cls(probe.forbidden_parent_local, probe.full)


async def test_managed_child_uses_declared_readonly_without_rebinding_parent_and_real_cas(
    mechanism,
):
    case, probe = mechanism, mechanism.probe
    parent = _callback(probe, "exact")
    origin = parent._origin
    initial_deadline = case.budget._deadline
    with pytest.raises(RuntimeError) as caught:
        await _invoke(case, callback=parent, observer=probe.observer)
    assert caught.value is probe.stop and probe.child.done()
    assert parent._origin is origin and origin[2] is probe.owner
    assert case.session._runtime_owner_token is case.owner_token
    assert case.budget._deadline == initial_deadline and probe.deadline <= initial_deadline
    assert probe.observed == case.base
    phases = {phase for mode, phase, _ in probe.events if mode == "observer"}
    assert {"native", "encode"} <= phases
    assert not phases & {"cas-write", "physical-cas", "file"}
    assert any(mode == "full" and phase == "file" for mode, phase, _ in probe.events)
    _assert_file_boundaries(probe, case.versions)
    assert sum(mode == "write" for mode, _, _ in probe.events) == 2
    bodies = {path.name: path.read_bytes() for path in case.store._blobs.iterdir()}
    with SQLiteWorkspaceTransactionStore(case.state, read_only=True) as reopened:
        parents = read_workspace_parent_closure(
            probe.observed, reopened._read_blob, checkpoint=lambda: None
        )
        assert len(parents) == 9
        assert {item.path for item in parents} == {
            ".",
            *(f"d{i}" for i in range(4)),
            *(f"d{i}/deep" for i in range(4)),
        }
        assert reopened._db.total_changes == 0
        assert bodies == {path.name: path.read_bytes() for path in reopened._blobs.iterdir()}
    # 已保存的真正 native 局部端口离段后回到 full，不留下借用权限。
    probe.events.clear()
    probe.saved[0]()
    assert any(mode == "full" for mode, _, _ in probe.events)
    assert not any(mode == "observer" for mode, _, _ in probe.events)


def _assert_file_boundaries(probe, versions):
    for path in versions:
        start = probe.events.index(("file-source-enter", "file", path))
        end = probe.events.index(("file-source-exit", "file", path))
        events = probe.events[start + 1 : end]
        controls = [event for event in events if event[0] in {"full", "observer"}]
        assert controls[0] == controls[-1] == ("full", "file", None)
        assert sum(mode == "full" for mode, _, _ in controls) == 2
        assert any(mode == "observer" and phase == "file-progress" for mode, phase, _ in controls)
        assert events.index(controls[0]) < next(
            index for index, event in enumerate(events) if event[0] == "file-io-enter"
        )
        assert max(index for index, event in enumerate(events) if event[0] == "file-io-exit") < (
            len(events) - 1 - events[::-1].index(controls[-1])
        )
        active_io = active_checkpoint = None
        for mode, phase, detail in events:
            if mode == "file-checkpoint-enter":
                assert active_io is None and active_checkpoint is None
                active_checkpoint = detail
            elif mode == "file-checkpoint-exit":
                assert detail == active_checkpoint and active_io is None
                active_checkpoint = None
            elif mode == "file-io-enter":
                assert phase == "file" and active_io is None and active_checkpoint is None
                active_io = detail
            elif mode == "file-io-exit":
                assert detail == active_io
                active_io = None
            elif mode == "observer":
                assert phase == "file-progress" and active_checkpoint == path and active_io is None
        assert active_io is None and active_checkpoint is None
        assert {detail[1] for mode, _, detail in events if mode == "file-io-enter"} >= {
            "open",
            "stat",
            "fstat",
            "read",
            "close",
        }


@pytest.mark.parametrize("operation", ["open", "stat", "fstat", "read", "close"])
async def test_actual_file_io_observer_mutation_still_trips_original_file_negative_control(
    mechanism, operation
):
    probe = mechanism.probe
    injected = False

    def inject(actual_operation, moment):
        nonlocal injected
        if actual_operation == operation and moment == "before" and not injected:
            injected = True
            probe.observer()

    probe.file_io_hook = inject
    with pytest.raises(AssertionError, match="cas-write"):
        await test_managed_child_uses_declared_readonly_without_rebinding_parent_and_real_cas(
            mechanism
        )
    assert injected and mechanism.probe.child.done()
    assert ("observer", "file", None) in probe.events
    assert any(
        mode == "file-io-enter" and detail[1] == operation for mode, _, detail in probe.events
    )


@pytest.mark.parametrize("boundary", ["entry", "exit"])
async def test_file_full_auth_observer_mutation_still_trips_original_file_negative_control(
    mechanism, boundary
):
    probe = mechanism.probe
    calls = 0
    injected = False

    def full():
        nonlocal calls, injected
        if probe.file_path is not None:
            calls += 1
            if calls == (1 if boundary == "entry" else 2):
                injected = True
                probe.observer()

    probe.full_hook = full
    with pytest.raises(AssertionError, match="cas-write"):
        await test_managed_child_uses_declared_readonly_without_rebinding_parent_and_real_cas(
            mechanism
        )
    assert injected and ("observer", "file", None) in probe.events


@pytest.mark.parametrize("name", _ERROR_NAMES)
async def test_first_file_checkpoint_error_keeps_identity_without_exit_auth_or_later_io(
    mechanism, name
):
    case, probe = mechanism, mechanism.probe
    error = _error(name)
    failed_at = None

    def observer():
        nonlocal failed_at
        if probe.phase == "file-progress":
            failed_at = len(probe.events)
            raise error

    def full():
        if failed_at is not None:
            pytest.fail("首文件 checkpoint 失败不得追加出口 full 认证")

    probe.observer_hook, probe.full_hook = observer, full
    with pytest.raises(BaseException) as caught:
        await _invoke(case, observer=probe.observer)
    assert caught.value is error and failed_at is not None and probe.child.done()
    assert not any(
        mode == "file-io-enter" and detail[1] != "close"
        for mode, _, detail in probe.events[failed_at:]
    )
    assert not any(mode in {"read", "physical-read"} for mode, _, _ in probe.events[failed_at:])


@pytest.mark.parametrize("location", ["checkpoint", "after-read"])
@pytest.mark.parametrize("signal", ["cancel", "budget"])
async def test_file_checkpoint_and_real_read_stop_before_next_io_on_original_signals(
    mechanism, location, signal
):
    case, probe = mechanism, mechanism.probe
    signalled_at = None

    def signal_once():
        nonlocal signalled_at
        if signalled_at is None:
            probe.record("file-signal", (location, signal))
            signalled_at = len(probe.events)
            if signal == "cancel":
                case.cancel.cancel()
            else:
                case.budget._deadline = time.monotonic() - 1

    def observer():
        if probe.phase == "file-progress" and location == "checkpoint":
            signal_once()

    def io(operation, moment):
        if operation == "read" and moment == "after" and location == "after-read":
            signal_once()

    probe.observer_hook, probe.file_io_hook = observer, io
    with pytest.raises(TurnCancelled if signal == "cancel" else KernelError) as caught:
        await _invoke(case, observer=probe.observer)
    assert signalled_at is not None and probe.child.done()
    if signal == "cancel":
        assert case.cancel.cancelled
    else:
        assert caught.value.code == "git_process_timeout"
    assert not any(
        mode == "file-io-enter" and detail[1] != "close"
        for mode, _, detail in probe.events[signalled_at:]
    )
    assert not any(
        mode == "full" and phase == "file" for mode, phase, _ in probe.events[signalled_at:]
    )
    assert sum(
        mode == "file-io-enter" and detail[1] == "read" for mode, _, detail in probe.events
    ) == (1 if location == "after-read" else 0)


async def test_saved_file_checkpoint_returns_to_full_after_original_segment(mechanism):
    case, probe = mechanism, mechanism.probe
    with pytest.raises(RuntimeError) as caught:
        await _invoke(case, observer=probe.observer)
    assert caught.value is probe.stop and probe.file_checkpoints
    probe.events.clear()
    probe.file_checkpoints[0]()
    assert any(mode == "full" for mode, _, _ in probe.events)
    assert not any(mode == "observer" for mode, _, _ in probe.events)


@pytest.mark.parametrize("kind", ["exact", "function", "proxy", "subclass"])
@pytest.mark.parametrize("explicit_none", [False, True])
async def test_default_none_and_unknown_callbacks_match_fixed_collector_complete_trace(
    mechanism, kind, explicit_none
):
    case, probe = mechanism, mechanism.probe
    callback = _callback(probe, kind)
    traces = []
    for implementation, options in (
        (_frozen_functions()["collect_product_git_user_observation"], {}),
        (user.collect_product_git_user_observation, {"explicit_none": explicit_none}),
    ):
        probe.events.clear()
        with pytest.raises(RuntimeError) as caught:
            await _invoke(case, implementation=implementation, callback=callback, **options)
        assert caught.value is probe.stop
        traces.append((tuple(probe.events), probe.observed.model_dump_json().encode()))
    assert traces[0] == traces[1]
    assert not any(mode == "observer" for mode, _, _ in traces[0][0])


@pytest.mark.parametrize("foreign", ["task", "thread"])
async def test_actual_foreign_creator_keeps_frozen_full_trace_with_none(mechanism, foreign):
    case, probe = mechanism, mechanism.probe
    parent = _callback(probe, "exact")
    origin = parent._origin
    results = []

    async def run(implementation):
        # 每次真实操作独占取消令牌；Event 不跨 asyncio.run 的不同循环复用。
        case.cancel = CancelToken()
        with pytest.raises(RuntimeError) as caught:
            await _invoke(
                case,
                implementation=implementation,
                callback=parent,
                explicit_none=implementation is user.collect_product_git_user_observation,
            )
        assert caught.value is probe.stop

    for implementation in (
        _frozen_functions()["collect_product_git_user_observation"],
        user.collect_product_git_user_observation,
    ):
        probe.events.clear()
        if foreign == "task":
            await asyncio.create_task(run(implementation))
        else:
            await asyncio.to_thread(
                lambda implementation=implementation: asyncio.run(run(implementation))
            )
        results.append((tuple(probe.events), probe.observed.model_dump_json().encode()))
    assert results[0] == results[1] and parent._origin is origin
    assert not any(mode == "observer" for mode, _, _ in results[0][0])


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass", "noncallable-observer"])
async def test_explicit_observer_rejects_unknown_control_before_authority_or_history(
    mechanism, kind
):
    case, probe = mechanism, mechanism.probe
    callback = _callback(probe, "exact" if kind == "noncallable-observer" else kind)
    observer = object() if kind == "noncallable-observer" else probe.observer
    with pytest.raises(KernelError) as caught:
        await _invoke(case, callback=callback, observer=observer)
    assert caught.value.code == "git_authentication_control_invalid" and not probe.events


@pytest.mark.parametrize("foreign", ["task", "thread"])
async def test_explicit_observer_rejects_actual_foreign_creator_before_read(mechanism, foreign):
    case, probe = mechanism, mechanism.probe
    parent = _callback(probe, "exact")
    origin = parent._origin

    async def run():
        with pytest.raises(KernelError) as caught:
            await _invoke(case, callback=parent, observer=probe.observer)
        assert caught.value.code == "git_authentication_control_invalid"

    if foreign == "task":
        await asyncio.create_task(run())
    else:
        await asyncio.to_thread(lambda: asyncio.run(run()))
    assert parent._origin is origin and not probe.events


@pytest.mark.parametrize("field", ["_local_check", "_authenticate", "_task", "_thread"])
async def test_declared_observer_rejects_source_binding_drift_before_observer(
    mechanism, monkeypatch, field
):
    case, probe = mechanism, mechanism.probe
    parent = _callback(probe, "exact")
    original = snapshots.capture_snapshot_facts

    def capture(*args, **kwargs):
        setattr(parent, field, object())
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "capture_snapshot_facts", capture)
    with pytest.raises(KernelError) as caught:
        await _invoke(case, callback=parent, observer=probe.observer)
    assert caught.value.code == "git_authentication_control_invalid"
    assert not any(mode in {"observer", "write", "encode"} for mode, _, _ in probe.events)


@pytest.mark.parametrize("phase", ["native", "encode"])
@pytest.mark.parametrize("name", _ERROR_NAMES)
async def test_first_native_observer_error_identity_has_no_exit_authentication_or_later_reads(
    mechanism, phase, name
):
    case, probe = mechanism, mechanism.probe
    error = _error(name)
    failed = False

    def observer():
        nonlocal failed
        if probe.phase == phase:
            failed = True
            raise error

    def full():
        if failed:
            pytest.fail("首局部失败不得触发出口 full 认证")

    probe.observer_hook, probe.full_hook = observer, full
    with pytest.raises(BaseException) as caught:
        await _invoke(case, observer=probe.observer)
    assert caught.value is error and failed
    assert probe.events[-1] == ("observer", phase, None)
    assert not any(mode in {"write", "read", "physical-read"} for mode, _, _ in probe.events)


@pytest.mark.parametrize("name", _ERROR_NAMES)
async def test_observer_cancel_then_raise_preserves_first_error_over_managed_cleanup(
    mechanism, name
):
    case, probe = mechanism, mechanism.probe
    error = _error(name)

    def observer():
        case.cancel.cancel()
        raise error

    probe.observer_hook = observer
    with pytest.raises(BaseException) as caught:
        await _invoke(case, observer=probe.observer)
    assert caught.value is error and case.cancel.cancelled and probe.child.done()
    assert probe.events[-1][0] == "observer"
    assert not any(mode in {"encode", "write", "physical-read"} for mode, _, _ in probe.events)


@pytest.mark.parametrize(
    "mutation",
    [
        "cancel",
        "budget",
        "audit",
        "transaction-checkpoint",
        "audit-checkpoint",
        "audit-read",
        "audit-and-callbacks",
    ],
)
async def test_native_observer_post_checks_reject_cancel_budget_and_original_reference_drift(
    mechanism, mutation
):
    case, probe = mechanism, mechanism.probe

    def observer():
        if mutation == "cancel":
            case.cancel.cancel()
        elif mutation == "budget":
            case.budget._deadline = time.monotonic() - 1
        elif mutation in {"audit", "audit-and-callbacks"}:
            case.router._audit = SimpleNamespace(
                _checkpoint=case.audit._checkpoint, _read_blob=case.audit._read_blob
            )
            if mutation == "audit-and-callbacks":
                # 新 Audit 代理不能先通过读取旧/新 callback 属性执行副作用。
                class HostileAudit:
                    @property
                    def _checkpoint(self):
                        pytest.fail("必须先拒绝 Audit 身份漂移")

                case.router._audit = HostileAudit()
        elif mutation == "transaction-checkpoint":
            case.store._checkpoint = lambda: None
        elif mutation == "audit-checkpoint":
            case.audit._checkpoint = lambda: None
        else:
            case.audit._read_blob = lambda _: b""

    probe.observer_hook = observer
    with pytest.raises(TurnCancelled if mutation == "cancel" else KernelError) as caught:
        await _invoke(case, observer=probe.observer)
    if mutation != "cancel":
        assert caught.value.code == (
            "git_process_timeout" if mutation == "budget" else "git_user_observation_host_invalid"
        )
    assert probe.events[-1][0] == "observer"
    assert not any(mode in {"encode", "write", "physical-read"} for mode, _, _ in probe.events)


async def test_owner_drift_during_native_is_rejected_at_exit_before_encoding(mechanism):
    case, probe = mechanism, mechanism.probe
    original_owner = case.session._runtime_owner_token
    error = KernelError("git_source_owner_changed", "original owner drift")

    def observer():
        case.session._runtime_owner_token = object()

    def authenticate():
        if case.session._runtime_owner_token is not original_owner:
            raise error

    probe.observer_hook, probe.full_hook = observer, authenticate
    with pytest.raises(KernelError) as caught:
        await _invoke(case, observer=probe.observer)
    assert caught.value is error
    assert probe.events[-1] == ("full", "native-exit", None)
    assert not any(mode in {"encode", "write", "physical-read"} for mode, _, _ in probe.events)


@pytest.mark.parametrize("failure", ["cancel", "budget"])
async def test_native_local_checks_original_signals_before_observer(
    mechanism, monkeypatch, failure
):
    case, probe = mechanism, mechanism.probe
    original = snapshots.capture_snapshot_facts

    def capture(*args, **kwargs):
        if failure == "cancel":
            case.cancel.cancel()
        else:
            case.budget._deadline = time.monotonic() - 1
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "capture_snapshot_facts", capture)
    with pytest.raises(TurnCancelled if failure == "cancel" else KernelError) as caught:
        await _invoke(case, observer=probe.observer)
    if failure == "budget":
        assert caught.value.code == "git_process_timeout"
    assert not any(mode in {"observer", "encode", "write"} for mode, _, _ in probe.events)


@pytest.mark.parametrize(
    "failure", ["cancel-before", "budget-before", "baseline-deadline", "owner-full"]
)
async def test_original_full_cancellation_deadline_and_owner_fail_before_next_native_read(
    mechanism, monkeypatch, failure
):
    case, probe = mechanism, mechanism.probe
    original = KernelError("git_source_owner_changed", "original owner error")
    if failure == "cancel-before":
        case.cancel.cancel()
    elif failure == "budget-before":
        case.budget._deadline = time.monotonic() - 1
    elif failure == "baseline-deadline":
        # 仅注入已过期的私有总截止点，不扩大原 60 秒/public 预算。
        monkeypatch.setattr(user, "_BASELINE_TIMEOUT_SECONDS", 0)
    else:
        probe.full_hook = lambda: (_ for _ in ()).throw(original)
    with pytest.raises(TurnCancelled if failure == "cancel-before" else KernelError) as caught:
        await _invoke(case, observer=probe.observer)
    if failure == "owner-full":
        assert caught.value is original
    assert not any(
        mode in {"history", "source", "native-read", "write"} for mode, _, _ in probe.events
    )


async def test_none_does_not_read_audit_and_authority_failure_precedes_callback_reads(
    mechanism, monkeypatch
):
    case, probe = mechanism, mechanism.probe

    class NoAudit:
        @property
        def _audit(self):
            pytest.fail("None 路线不能访问 Audit")

    case.router = NoAudit()
    with pytest.raises(RuntimeError) as caught:
        await _invoke(case, explicit_none=True)
    assert caught.value is probe.stop
    probe.events.clear()
    # authority 明确失败：不伪造完整来源成功，也不读取未认证 callbacks。
    stop = OSError("authority first error")

    def reject_authority(*_):
        raise stop

    monkeypatch.setattr(user, "require_git_user_authority", reject_authority)
    for observer in (None, probe.observer):
        with pytest.raises(OSError) as caught:
            await _invoke(case, observer=observer, explicit_none=observer is None)
        assert caught.value is stop
    assert not probe.events


@pytest.mark.parametrize("planner_kind", ["exact", "subclass"])
async def test_preparer_entry_hands_only_exact_planner_frozen_readonly_check_to_managed_child(
    mechanism, monkeypatch, planner_kind
):
    case, probe = mechanism, mechanism.probe
    owner = asyncio.current_task()
    stop = RuntimeError("stop at prepare handoff, not fake SDK success")
    frozen_resources = (case.session, case.store, case.router, case.reader, case.ports)

    class Subclass(preparation.ProductGitCheckpointPreparer):
        pass

    cls = preparation.ProductGitCheckpointPreparer if planner_kind == "exact" else Subclass
    planner = cls.__new__(cls)
    planner.core_store = ProductGitDeliveryCoreStore(case.store)
    planner.material_port = GitDeliveryProcess.__new__(GitDeliveryProcess)

    def local():
        assert all(
            original is current
            for original, current in zip(
                frozen_resources,
                (case.session, case.store, case.router, case.reader, case.ports),
                strict=True,
            )
        )
        probe.record("preparer-readonly")

    def authenticate():
        probe.record("preparer-full")

    # 显式原控制工厂 fixture：两个端口分别冻结资源，不从任意 GitControl 借局部。
    def control_factory(actual_planner, context, cancel, budget, failures):
        assert actual_planner is planner and cancel is case.cancel
        assert asyncio.current_task() is owner
        return local, authenticate

    async def prepare_handoff(*args):
        actual_planner, check, readonly = args[0], args[-2], args[-1]
        assert actual_planner is planner and asyncio.current_task() is not owner
        if planner_kind == "exact":
            assert type(check) is GitAuthenticationControl
            assert check._origin[2] is asyncio.current_task() and readonly is local
            assert readonly is not check
            readonly()
        else:
            assert check is authenticate and readonly is None
        raise stop

    monkeypatch.setattr(preparation, "_preparation_control", control_factory)
    monkeypatch.setattr(preparation, "_prepare", prepare_handoff)
    context = ActionPlanningContext(case.root, object(), object(), snapshot_ports=case.ports)
    with pytest.raises(RuntimeError) as caught:
        await preparation._prepare_entry(
            planner, object(), object(), context, case.thread, object(), object(), case.cancel
        )
    assert caught.value is stop
    assert [mode for mode, _, _ in probe.events] == (
        ["preparer-full", "preparer-readonly"] if planner_kind == "exact" else ["preparer-full"]
    )


def _baseline(case, oid_length):
    mutations = tuple(
        WorkspaceMutation(path=path, before=before, after=after)
        for path, (before, after) in sorted(case.versions.items())
    )
    candidate = ProductGitDeliverySourceV2.model_construct(
        thread_id=case.thread.thread_id,
        patches=(
            WorkspacePatchSourceReference(
                turn_id=UUID(int=1),
                call_id=UUID(int=2),
                transaction_id=UUID(int=3),
                route_fingerprint="1" * 64,
                transaction_fingerprint="2" * 64,
            ),
        ),
        workspace=case.base,
        mutations=mutations,
        digest="0" * 64,
    )
    selected = ProductGitDeliverySourceV2(
        **candidate.model_dump(exclude={"digest"}),
        digest=product_git_delivery_source_digest(candidate),
    )
    candidate = ProductGitDeliveryBaselineV2.model_construct(
        source=selected,
        head_oid="a" * oid_length,
        head_tree_oid="b" * oid_length,
        head_ref="refs/heads/main" if oid_length == 40 else "HEAD",
        index_observation_sha256="3" * 64,
        index_observation_bytes=0,
        status_sha256="4" * 64,
        config_names_sha256="5" * 64,
        reader_binding="6" * 64,
        members=tuple(GitBaselineMember(path=item.path) for item in mutations),
        digest="0" * 64,
    )
    return ProductGitDeliveryBaselineV2(
        **candidate.model_dump(exclude={"digest"}), digest=product_git_baseline_digest(candidate)
    )


@pytest.mark.parametrize("oid_length,index_present", [(40, False), (40, True), (64, True)])
def test_pure_builder_matches_fixed_old_ast_model_bytes_and_fingerprint(
    mechanism, monkeypatch, oid_length, index_present
):
    case = mechanism
    baseline = _baseline(case, oid_length)
    pinned = PinnedGitUserDirectories(case.root, case.root / "d0", object(), "7" * 64, "8" * 64)
    facts = git_user_directory_facts(pinned)
    index = GitIndexFileObservation(
        presence="file" if index_present else "absent",
        identity="9" * 64 if index_present else None,
        sha256="a" * 64 if index_present else None,
        size=17 if index_present else 0,
    )
    old = _frozen_functions()["_observation"](
        baseline, pinned, index, "b" * 64, "c" * 64, UUID(int=4), UUID(int=5)
    )

    def no_io(*args, **kwargs):
        pytest.fail("纯 builder 不应读取路径/CAS/文件")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", no_io)
        patch.setattr(Path, "open", no_io)
        actual = contracts.build_product_git_user_observation(
            baseline, facts, index, "b" * 64, "c" * 64, UUID(int=4), UUID(int=5)
        )
    assert actual.model_dump_json().encode() == old.model_dump_json().encode()
    assert actual.fingerprint == old.fingerprint
    assert (
        contracts.ProductGitUserObservation.model_validate_json(
            actual.model_dump_json(), strict=True
        )
        == actual
    )
