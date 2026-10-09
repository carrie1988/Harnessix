"""原 proof 首轮 Route 读取接线；不声称完成 Session/MAC 或 SDK 业务验收。

公开消费入口在真实 Route 深快照后、首个 Core Reader 入口被 sentinel 截止。
父闭包读取、CAS 摘要校验、SQLite 与 Store 观察全部沿用原实现。
不导入新增私有 helper：旧安装件可 collect，分层缺失由实际轨迹断言报 red。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config import git_approval_history_proof, git_prepared_link_proof
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2, ActionRouteSnapshotV2
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from harnessix.workspace.terminal_read_control import terminal_read_scope
from tests.trusted_actions.test_parent_closure_store import parent_route

CONSUMERS = ("prepared", "approval")
CONTROL_ERRORS = ("os", "sqlite", "same-code", "nested", "task-cancel", "timeout")
OBSERVATIONS = {
    "audit-observe",
    "workspace-observe",
    "terminal-full",
    "sql",
    "cas-start",
    "cas-end",
}


class _ReachedCore(Exception):
    """仅表示原 Route 读取与原深快照已经返回，不表示 Core 认证成功。"""


@dataclass(frozen=True)
class _Event:
    name: str
    phase: str
    in_pure: bool
    detail: str = ""


class _Probe:
    def __init__(self):
        self.phase = "setup"
        self.events = []
        self.status_calls = []
        self.status_results = []
        self.read_control = None
        self.last_read_control = None
        self.hits = Counter()
        self.failure_at = None
        self.failure = None
        self.hook = None
        self.drift = False
        self.drift_error = None
        self.control = GitAuthenticationControl(self.local, self.full)

    def record(self, name, detail=""):
        if self.phase == "setup":
            return
        control = self.read_control if self.phase == "status" else self.control
        event = _Event(
            name,
            self.phase,
            type(control) is GitAuthenticationControl and control._segment is not None,
            detail,
        )
        self.events.append(event)
        if self.phase == "status":
            self.hits[name] += 1
            if (name, self.hits[name]) == self.failure_at:
                raise self.failure
        if self.hook is not None:
            self.hook(event)

    def full(self):
        self.record("full")
        if self.drift:
            raise self.drift_error

    def local(self):
        self.record("local")

    def audit_observe(self):
        self.record("audit-observe")

    def workspace_observe(self):
        self.record("workspace-observe")

    def terminal_full(self):
        self.record("terminal-full")

    def sql(self, statement):
        # 只记录实际 SQL；去掉独立夹具不同的 UUID，不替换连接或执行算法。
        self.record("sql", statement.partition(" WHERE")[0])


@dataclass
class _Case:
    route: ActionRoutePlanV2
    expected: ActionRouteSnapshotV2
    store: SQLiteWorkspaceTransactionStore
    audit: SQLiteActionAuditStore
    plans: SQLiteExecutionPlanStore
    router: TrustedActionRouter
    probe: _Probe
    expected_reads: tuple[str, ...]
    blob_labels: dict[str, str]
    workspace_observer: object
    audit_observer: object
    db_before: tuple
    blobs_before: tuple
    core_marker: _ReachedCore = field(default_factory=_ReachedCore)
    core_calls: list = field(default_factory=list)

    def blob_state(self):
        return tuple(
            (digest, path.read_bytes() if path.exists() else None)
            for digest in self.expected_reads
            for path in (self.store._blobs / digest,)
        )

    def assert_unchanged(self):
        assert _database_state(self.store, self.audit, self.plans) == self.db_before
        assert self.blob_state() == self.blobs_before
        assert self.store._checkpoint is self.workspace_observer
        assert self.audit._checkpoint is self.audit_observer
        assert all(not item._db.in_transaction for item in (self.store, self.audit, self.plans))

    def trace(self, *, controls=False):
        names = OBSERVATIONS | ({"full", "local"} if controls else set())
        return [
            (event.name, self.blob_labels.get(event.detail, event.detail))
            for event in self.probe.events
            if event.phase == "status" and event.name in names
        ]


def _database_state(*stores):
    return tuple((item._db.total_changes, tuple(item._db.iterdump())) for item in stores)


@pytest.fixture
def case_factory(tmp_path, monkeypatch):
    serial = 0

    @contextmanager
    def make(probe=None):
        nonlocal serial
        root = tmp_path / f"case-{serial}"
        serial += 1
        root.mkdir()
        probe = _Probe() if probe is None else probe
        route, blobs = parent_route(root / "workspace", leaves=3)
        reference = route.execution.workspace.parent_closure
        manifest = json.loads(blobs[reference.sha256])
        expected_reads = (reference.sha256, *(item["sha256"] for item in manifest["chunks"]))
        assert set(expected_reads) == set(blobs)
        labels = {reference.sha256: "manifest"}
        labels.update({digest: f"chunk-{index}" for index, digest in enumerate(expected_reads[1:])})
        workspace_observer = probe.workspace_observe
        audit_observer = probe.audit_observe
        with ExitStack() as stack:
            store = stack.enter_context(
                SQLiteWorkspaceTransactionStore(root / "cas", checkpoint=workspace_observer)
            )
            for digest, body in blobs.items():
                store.put_blob(digest, body)

            def read_cas(digest):
                probe.record("cas-start", digest)
                try:
                    return store.blob(digest)
                finally:
                    probe.record("cas-end", digest)

            plans = stack.enter_context(
                SQLiteExecutionPlanStore(root / "plans.db", read_blob=read_cas)
            )
            plans.save_plan(route.execution)
            audit = stack.enter_context(
                SQLiteActionAuditStore(
                    root / "audit.db", read_blob=read_cas, checkpoint=audit_observer
                )
            )
            expected = audit.save_plan(route, initial_state="pending_approval")
            router = TrustedActionRouter(
                plans=plans, audit=audit, workspace_root=lambda _: root / "workspace"
            )
            original_status = router.status

            def status(route_id, **kwargs):
                # kwargs 原对象逐项转发；不包装 pure 工厂、不改 Store 共享观察属性。
                probe.status_calls.append((route_id, kwargs.copy()))
                previous = probe.phase, probe.read_control
                probe.phase, probe.read_control = "status", kwargs.get("checkpoint")
                probe.last_read_control = probe.read_control
                try:
                    result = original_status(route_id, **kwargs)
                    probe.status_results.append(result)
                    return result
                finally:
                    probe.phase, probe.read_control = previous

            monkeypatch.setattr(router, "status", status)
            case = _Case(
                route=route,
                expected=expected,
                store=store,
                audit=audit,
                plans=plans,
                router=router,
                probe=probe,
                expected_reads=expected_reads,
                blob_labels=labels,
                workspace_observer=workspace_observer,
                audit_observer=audit_observer,
                db_before=_database_state(store, audit, plans),
                blobs_before=(),
            )
            case.blobs_before = case.blob_state()
            for item in (store, audit, plans):
                item._db.set_trace_callback(probe.sql)
            probe.phase = "consumer"
            try:
                yield case
            finally:
                for item in (store, audit, plans):
                    item._db.set_trace_callback(None)

    return make


async def _consume(case, consumer, checkpoint, monkeypatch):
    module = git_prepared_link_proof if consumer == "prepared" else git_approval_history_proof

    def stop_at_core(core_store, route, *, checkpoint):
        case.core_calls.append((core_store, route, checkpoint))
        case.probe.record("core")
        raise case.core_marker

    monkeypatch.setattr(module, "load_product_git_delivery_route_core_v2", stop_at_core)
    arguments = (
        case.router,
        ProductGitDeliveryCoreStore(case.store),
        None,  # sentinel 之前不读取 Artifact/Session；不伪造认证资源。
        WorkspaceSnapshotPorts(case.store.put_blob, case.store.blob),
        "route-reader-only",
    )
    controls = {"cancel": CancelToken(), "budget": None, "checkpoint": checkpoint}
    if consumer == "prepared":
        return await module.authenticate_prepared_link(
            case.route.execution.plan_id, *arguments, **controls
        )
    prepared = SimpleNamespace(plan=SimpleNamespace(route=case.route))
    return await module.read_original_approval_evidence(prepared, *arguments, **controls)


async def _until_core(case, consumer, checkpoint, monkeypatch):
    with pytest.raises(_ReachedCore) as caught:
        await _consume(case, consumer, checkpoint, monkeypatch)
    assert caught.value is case.core_marker
    assert len(case.core_calls) == 1
    assert case.core_calls[0][1] == case.route
    assert case.core_calls[0][1] is not case.probe.status_results[0].plan


def _legacy(case, checkpoint):
    # 比较对象是原 status 的旧调用形状，不在测试中重写读取算法。
    assert case.router.status(case.route.execution.plan_id, checkpoint=checkpoint) == case.expected
    case.assert_unchanged()


def _status_events(case):
    return [event for event in case.probe.events if event.phase == "status"]


def _assert_full_io_and_complete_cas(case):
    events = _status_events(case)
    reads = [event.detail for event in events if event.name == "cas-start"]
    assert tuple(reads) == case.expected_reads
    assert [event.detail for event in events if event.name == "cas-end"] == reads
    assert any(event.name == "sql" for event in events)
    assert all(
        not event.in_pure
        for event in events
        if event.name in {"sql", "cas-start", "cas-end", "workspace-observe", "full"}
    )
    for index, event in enumerate(events):
        if event.name != "cas-start":
            continue
        end = next(i for i in range(index + 1, len(events)) if events[i].name == "cas-end")
        before_read = [item for item in events[:index] if item.name in {"full", "local"}]
        after_read = [item for item in events[end + 1 :] if item.name in {"full", "local"}]
        assert before_read[-1].name == "full" and not before_read[-1].in_pure
        assert after_read[0].name == "full" and not after_read[0].in_pure
    case.assert_unchanged()


def _assert_old_fallback(case, checkpoint, expected_trace):
    assert len(case.probe.status_calls) == 1
    kwargs = case.probe.status_calls[0][1]
    assert set(kwargs) == {"checkpoint"}
    assert kwargs["checkpoint"] is checkpoint
    assert case.trace(controls=True) == expected_trace
    assert not any(event.name == "local" or event.in_pure for event in _status_events(case))
    _assert_full_io_and_complete_cas(case)


@pytest.mark.parametrize("consumer", CONSUMERS)
async def test_public_proof_keeps_original_route_complete_cas_and_every_store_observation(
    case_factory, monkeypatch, consumer
):
    with case_factory() as baseline:
        _legacy(baseline, baseline.probe.full)
        old_observations = baseline.trace()
    with case_factory() as case:
        await _until_core(case, consumer, case.probe.control, monkeypatch)
        assert case.probe.status_results == [case.expected]
        assert case.trace() == old_observations
        kwargs = case.probe.status_calls[0][1]
        assert set(kwargs) == {"checkpoint", "pure_progress"}
        child = kwargs["checkpoint"]
        assert type(child) is GitAuthenticationControl and child is not case.probe.control
        assert (child._task, child._thread) == (
            case.probe.control._task,
            case.probe.control._thread,
        )
        events = _status_events(case)
        locals_at = [index for index, event in enumerate(events) if event.name == "local"]
        assert locals_at, "proof 原 Route 父闭包没有消费局部 progress"
        for index in locals_at:
            assert events[index].in_pure
            # 段退出的末次 local 是控制原实现的追加检查，不是一个历史观察点。
            if index and events[index - 1].name == "audit-observe":
                assert events[index - 1].in_pure
        assert any(event.name == "audit-observe" and event.in_pure for event in events)
        assert child._segment is None and case.probe.control._segment is None
        _assert_full_io_and_complete_cas(case)


def _error(kind):
    if kind == "os":
        return OSError("original progress control")
    if kind == "sqlite":
        return sqlite3.OperationalError("original progress control")
    if kind == "same-code":
        return KernelError("action_audit_store_corrupt", "control, not damaged data")
    if kind == "nested":
        return UpstreamCheckpointError(UpstreamCheckpointError(OSError("original control")))
    if kind == "task-cancel":
        return asyncio.CancelledError("original task cancellation")
    if kind == "domain-cancel":
        return TurnCancelled()
    assert kind == "timeout"
    return TimeoutError("original deadline")


def _failure_target(sample, phase):
    events = _status_events(sample)
    locals_at = [index for index, event in enumerate(events) if event.name == "local"]
    assert locals_at, "旧安装件缺少 proof 原 Route 分层，不是 collect/import 失败"
    first = locals_at[0]
    exit_at = next(index for index in range(first + 1, len(events)) if events[index].name == "full")
    segment_locals = [index for index in locals_at if index < exit_at]
    middle = segment_locals[len(segment_locals) // 2]
    if phase == "entry-full":
        index = max(index for index in range(first) if events[index].name == "full")
    elif phase == "local":
        index = middle
    elif phase == "original-observation":
        index = middle - 1
        assert events[index].name == "audit-observe" and events[index].in_pure
    else:
        assert phase == "exit-full"
        index = exit_at
    name = events[index].name
    ordinal = sum(event.name == name for event in events[: index + 1])
    return (name, ordinal), events[: index + 1]


async def _assert_first_failure(case_factory, monkeypatch, consumer, phase, error_kind):
    with case_factory() as sample:
        await _until_core(sample, consumer, sample.probe.control, monkeypatch)
        target, expected_prefix = _failure_target(sample, phase)
    with case_factory() as case:
        marker = _error(error_kind)
        case.probe.failure_at, case.probe.failure = target, marker
        with pytest.raises(type(marker)) as caught:
            await _consume(case, consumer, case.probe.control, monkeypatch)
        assert caught.value is marker
        assert case.core_calls == [] and case.probe.status_results == []
        actual = _status_events(case)
        assert [(event.name, event.in_pure) for event in actual] == [
            (event.name, event.in_pure) for event in expected_prefix
        ]
        assert actual[-1].name == target[0]
        assert case.probe.control._segment is None
        assert case.probe.last_read_control._segment is None
        reads = tuple(event.detail for event in actual if event.name == "cas-start")
        assert reads == case.expected_reads[: len(reads)]
        case.assert_unchanged()


@pytest.mark.parametrize("error_kind", CONTROL_ERRORS)
@pytest.mark.parametrize(
    "consumer,phase",
    [
        ("prepared", "entry-full"),
        ("prepared", "local"),
        ("approval", "original-observation"),
        ("approval", "exit-full"),
    ],
)
async def test_first_error_preserves_object_and_stops_before_next_read_or_core(
    case_factory, monkeypatch, consumer, phase, error_kind
):
    await _assert_first_failure(case_factory, monkeypatch, consumer, phase, error_kind)


@pytest.mark.parametrize(
    "consumer,phase",
    [
        ("approval", "entry-full"),
        ("approval", "local"),
        ("prepared", "original-observation"),
        ("prepared", "exit-full"),
    ],
)
async def test_other_public_consumer_preserves_domain_cancellation_at_each_boundary(
    case_factory, monkeypatch, consumer, phase
):
    await _assert_first_failure(case_factory, monkeypatch, consumer, phase, "domain-cancel")


def _unknown_callback(probe, kind):
    def forbidden(*_args, **_kwargs):
        pytest.fail("未知回调不得借用其 pure/io_progress 工厂")

    def function():
        probe.full()

    function.pure = function.io_progress = forbidden

    class Methods:
        pure = io_progress = staticmethod(forbidden)

        def checkpoint(self):
            probe.full()

    class Proxy:
        pure = io_progress = staticmethod(forbidden)

        def __call__(self):
            probe.full()

    class Subclass(GitAuthenticationControl):
        pure = io_progress = staticmethod(forbidden)

    return {
        "function": function,
        "method": Methods().checkpoint,
        "proxy": Proxy(),
        "subclass": Subclass(probe.local, probe.full),
    }[kind]


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("kind", ("function", "method", "proxy", "subclass"))
async def test_unknown_callbacks_keep_original_kwargs_identity_and_full_trace(
    case_factory, monkeypatch, consumer, kind
):
    with case_factory() as baseline:
        _legacy(baseline, baseline.probe.full)
        expected_trace = baseline.trace(controls=True)
    with case_factory() as case:
        checkpoint = _unknown_callback(case.probe, kind)
        await _until_core(case, consumer, checkpoint, monkeypatch)
        _assert_old_fallback(case, checkpoint, expected_trace)


@pytest.mark.parametrize("consumer", CONSUMERS)
async def test_foreign_task_exact_control_keeps_original_object_and_full_call_count(
    case_factory, monkeypatch, consumer
):
    with case_factory() as baseline:
        _legacy(baseline, baseline.probe.full)
        expected_trace = baseline.trace(controls=True)
    with case_factory() as case:
        checkpoint = case.probe.control
        owner, origin = asyncio.current_task(), checkpoint._origin

        async def child():
            assert asyncio.current_task() is not owner
            await _until_core(case, consumer, checkpoint, monkeypatch)

        await asyncio.create_task(child(), context=copy_context())
        assert checkpoint._origin is origin and checkpoint._task is owner
        _assert_old_fallback(case, checkpoint, expected_trace)


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_foreign_thread_exact_control_keeps_original_object_and_full_call_count(
    case_factory, monkeypatch, consumer
):
    with case_factory() as baseline:
        _legacy(baseline, baseline.probe.full)
        expected_trace = baseline.trace(controls=True)
    probe = _Probe()
    origin = probe.control._origin
    assert probe.control._task is None

    def worker():
        # 首轮公开入口在第一次异步 I/O 前已遇 sentinel；同步推进保持 Task=None，
        # 因而这是真正单独的 foreign thread 负控，不借 foreign Task 掩盖线程检查。
        with case_factory(probe) as case:
            operation = _consume(case, consumer, probe.control, monkeypatch)
            try:
                with pytest.raises(_ReachedCore) as caught:
                    operation.send(None)
                assert caught.value is case.core_marker
            finally:
                operation.close()
            assert case.core_calls[0][1] == case.route
            _assert_old_fallback(case, probe.control, expected_trace)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(copy_context().run, worker).result(timeout=15)
    assert probe.control._origin is origin and probe.control._segment is None


def _capture_original_io_checks(monkeypatch):
    """只保留原 context manager 实际交付的 check；不制造测试版 progress。"""
    saved = []
    original = GitAuthenticationControl.io_progress

    @contextmanager
    def capture(control):
        with original(control) as check:
            saved.append((control, check))
            yield check

    monkeypatch.setattr(GitAuthenticationControl, "io_progress", capture)
    return saved


@pytest.mark.parametrize("consumer", CONSUMERS)
async def test_saved_route_local_check_is_full_after_original_segment_exit(
    case_factory, monkeypatch, consumer
):
    saved = _capture_original_io_checks(monkeypatch)
    with case_factory() as case:
        await _until_core(case, consumer, case.probe.control, monkeypatch)
        assert saved, "原 Route 没有交付真实 io_progress 检查点"
        child = case.probe.status_calls[0][1]["checkpoint"]
        assert all(control is child for control, _check in saved)
        for control, check in saved:
            assert control._segment is None
            before = len(case.probe.events)
            check()
            assert [event.name for event in case.probe.events[before:]] == ["full"]
        case.assert_unchanged()


@pytest.mark.parametrize("consumer", CONSUMERS)
async def test_child_full_reentry_revokes_only_its_actual_route_local_segment(
    case_factory, monkeypatch, consumer
):
    saved = _capture_original_io_checks(monkeypatch)
    with case_factory() as case:
        reentry_trace = []
        triggered = False

        def reenter(event):
            nonlocal triggered
            if triggered or event.name != "audit-observe" or not event.in_pure:
                return
            triggered = True
            assert saved
            child, check = saved[-1]
            assert child is case.probe.read_control and child is not case.probe.control
            assert child._segment is not None
            before = len(case.probe.events)
            child()  # 只有派生 child 的 Full 撤销 childtoken，不把父 Full 混称为撤销。
            assert child._segment is None
            check()
            check()
            reentry_trace.extend(event.name for event in case.probe.events[before:])

        case.probe.hook = reenter
        await _until_core(case, consumer, case.probe.control, monkeypatch)
        assert triggered, "原 Route 没有进入可验证的 child 局部段"
        assert reentry_trace == ["full", "full", "full"]
        assert case.probe.status_results == [case.expected]
        assert saved[0][0]._segment is None
        _assert_full_io_and_complete_cas(case)


@pytest.mark.parametrize("consumer", CONSUMERS)
async def test_persistent_authentication_drift_is_rejected_by_original_segment_exit_full(
    case_factory, monkeypatch, consumer
):
    with case_factory() as case:
        marker = KernelError("git_user_observation_host_invalid", "persistent original drift")
        case.probe.drift_error = marker

        def drift_after_local(event):
            if event.phase == "status" and event.name == "local":
                case.probe.drift = True

        case.probe.hook = drift_after_local
        with pytest.raises(KernelError) as caught:
            await _consume(case, consumer, case.probe.control, monkeypatch)
        assert caught.value is marker and case.probe.drift
        events = _status_events(case)
        assert any(event.name == "local" for event in events)
        assert events[-1].name == "full" and not events[-1].in_pure
        assert case.core_calls == [] and case.probe.status_results == []
        assert case.probe.control._segment is None
        assert case.probe.last_read_control._segment is None
        case.assert_unchanged()


def _damage_physical_cas(case, damage):
    digest = case.expected_reads[0 if damage.startswith("manifest") else 1]
    path = case.store._blobs / digest
    if damage.endswith("missing"):
        path.unlink()
    else:
        before = path.read_bytes()
        changed = bytes((before[0] ^ 1,)) + before[1:]
        assert len(changed) == len(before) and changed != before
        path.write_bytes(changed)
    case.blobs_before = case.blob_state()


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize(
    "damage", ("manifest-missing", "chunk-missing", "manifest-same-length", "chunk-same-length")
)
async def test_real_missing_or_same_length_cas_damage_keeps_old_data_classification(
    case_factory, monkeypatch, consumer, damage
):
    with case_factory() as baseline:
        _damage_physical_cas(baseline, damage)
        with pytest.raises(KernelError) as original:
            baseline.router.status(baseline.route.execution.plan_id, checkpoint=baseline.probe.full)
        assert original.value.code == "action_audit_store_corrupt"
        old_trace = baseline.trace(controls=True)
        baseline.assert_unchanged()
    with case_factory() as case:
        _damage_physical_cas(case, damage)
        with pytest.raises(KernelError) as caught:
            await _consume(case, consumer, case.probe.control, monkeypatch)
        assert caught.value.code == original.value.code
        assert caught.value.message == original.value.message
        assert case.trace(controls=True) == old_trace
        reads = tuple(event.detail for event in _status_events(case) if event.name == "cas-start")
        assert reads == case.expected_reads[: len(reads)]
        assert case.core_calls == [] and case.probe.status_results == []
        case.assert_unchanged()


@contextmanager
def _original_terminal_scope(case):
    def physical_read(digest):
        case.probe.record("cas-start", digest)
        try:
            return case.store._read_blob(digest)
        finally:
            case.probe.record("cas-end", digest)

    with terminal_read_scope(case.store, case.audit, physical_read, case.probe.terminal_full):
        yield


@pytest.mark.parametrize("consumer", CONSUMERS)
async def test_real_terminal_scope_keeps_internal_full_at_every_original_observation_point(
    case_factory, monkeypatch, consumer
):
    with case_factory() as baseline:
        with _original_terminal_scope(baseline):
            _legacy(baseline, baseline.probe.full)
        old_observations = baseline.trace()
    with case_factory() as case:
        with _original_terminal_scope(case):
            await _until_core(case, consumer, case.probe.control, monkeypatch)
        assert case.trace() == old_observations
        events = _status_events(case)
        assert any(event.name == "local" and event.in_pure for event in events)
        assert any(event.name == "terminal-full" and event.in_pure for event in events)
        assert not any(event.name in {"audit-observe", "workspace-observe"} for event in events)
        _assert_full_io_and_complete_cas(case)
