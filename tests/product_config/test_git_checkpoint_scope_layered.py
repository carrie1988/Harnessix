"""真实临时 SQLite CAS 的 Scope 入参纯段；控制替身不证明 SDK 来源认证。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import git_checkpoint_scope as assembly
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.product_config.test_git_checkpoint_scope import build, inputs
from tests.product_config.test_git_checkpoint_scope import cas as cas
from tests.product_config.test_git_checkpoint_scope import case as case
from tests.support.git_delivery_observed_core import canonical, json_facts, make_observed_case

ERROR_KINDS = (
    "os",
    "value",
    "kernel",
    "timeout",
    "cancel",
    "task-cancel",
    "upstream",
    "nested-upstream",
    "drift",
    "deadline",
)


def _error(kind):
    return {
        "os": OSError("original control"),
        "value": ValueError("original control"),
        "kernel": KernelError("original_control", "original control"),
        "timeout": TimeoutError("original control"),
        "cancel": TurnCancelled(),
        "task-cancel": asyncio.CancelledError("original control"),
        "upstream": UpstreamCheckpointError(ValueError("original control")),
        "nested-upstream": UpstreamCheckpointError(
            UpstreamCheckpointError(ValueError("original control"))
        ),
        "drift": KernelError("git_user_observation_host_invalid", "original drift"),
        "deadline": KernelError("time_budget_exceeded", "original deadline"),
    }[kind]


class _Probe:
    def __init__(self):
        self.phase = "entry"
        self.in_pure = False
        self.events = []
        self.scopes = 0
        self.retired = None
        self.build_control = None
        self.failure_at = None
        self.failure = None
        self.control = GitAuthenticationControl(self.local, self.authenticate)

    def record(self, channel):
        event = (self.phase, channel)
        self.events.append(event)
        if event == self.failure_at:
            raise self.failure

    def local(self):
        self.record("local")

    def authenticate(self):
        self.record("auth")


def _observe(monkeypatch, case, probe):
    def watch(target, name, phase, *, outside=False):
        original = getattr(target, name)

        def observed(*args, **kwargs):
            previous = probe.phase
            actual_phase = phase(*args, previous=previous, **kwargs) if callable(phase) else phase
            probe.phase = actual_phase
            probe.events.append((actual_phase, "enter"))
            try:
                if outside:
                    assert not probe.in_pure and probe.control._segment is None
                    if type(probe.build_control) is GitAuthenticationControl:
                        assert probe.build_control._segment is None
                return original(*args, **kwargs)
            finally:
                probe.events.append((actual_phase, "leave"))
                probe.phase = previous

        monkeypatch.setattr(target, name, observed)

    def model_phase(_value, kind, _check, *, previous):
        if kind is GitTreeClosureLimits:
            return "limits"
        return previous if previous in {"base-catalog", "after-catalog"} else "reference"

    catalog_count = 0

    def catalog_phase(*args, previous, **kwargs):
        nonlocal catalog_count
        catalog_count += 1
        return "base-catalog" if catalog_count % 2 else "after-catalog"

    watch(assembly, "_snapshot", "baseline")
    watch(assembly, "_snapshot_model", model_phase)
    watch(assembly, "_catalog", catalog_phase)
    for name in (
        "_build",
        "_scope",
        "parse_git_commit",
        "parse_git_tree",
        "prepare_git_tree_diff",
        "verify_git_tree_closure",
        "snapshot_git_inventory_scope",
        "verify_git_inventory_scope_materials",
    ):
        watch(assembly, name, name, outside=True)
    for name in ("read", "persist"):
        watch(GitMaterialCAS, name, "cas-" + name, outside=True)
    for name in ("put_blob", "blob", "_put_blob", "_read_blob", "_check"):
        watch(case.cas.store, name, name, outside=True)
    original_pure = getattr(assembly, "pure_git_authentication", None)
    if original_pure is None:
        return

    @contextmanager
    def pure(checkpoint):
        probe.scopes += 1
        probe.phase = "pure-enter"
        with original_pure(checkpoint) as check:
            probe.retired = check
            probe.phase = "inputs"
            probe.in_pure = True
            try:
                yield check
                probe.phase = "pure-exit"
            finally:
                probe.in_pure = False
        probe.phase = "entry"

    monkeypatch.setattr(assembly, "pure_git_authentication", pure)
    original_control = assembly._build_control

    def build_control(*args):
        probe.build_control = original_control(*args)
        return probe.build_control

    monkeypatch.setattr(assembly, "_build_control", build_control)


def test_only_declarative_inputs_are_layered_and_canonical_scope_diff_unchanged(case, monkeypatch):
    probe = _Probe()
    _observe(monkeypatch, case, probe)
    before = canonical(json_facts(inputs(case)))
    rows = tuple(case.cas.store._db.iterdump())
    scope, diff = build(case, checkpoint=probe.control)
    assert probe.scopes == 1
    for phase in ("baseline", "reference", "base-catalog", "after-catalog", "limits"):
        assert (phase, "local") in probe.events
        assert (phase, "auth") not in probe.events
    assert probe.events.count(("pure-enter", "auth")) == 1
    assert probe.events.count(("pure-exit", "auth")) == 1
    for phase in ("_build", "parse_git_commit", "_scope"):
        assert (phase, "auth") in probe.events
        assert (phase, "local") not in probe.events
    for phase in ("snapshot_git_inventory_scope", "verify_git_inventory_scope_materials"):
        assert (phase, "auth") in probe.events and (phase, "local") in probe.events
    assert ("prepare_git_tree_diff", "auth") in probe.events
    assert canonical(json_facts(scope)) == canonical(json_facts(case.core.object_scope))
    assert diff.content.text.encode() == case.diff_text.encode()
    assert (diff.content.sha256, diff.content.utf8_bytes) == (
        case.core.diff_sha256,
        case.core.diff_bytes,
    )
    assert canonical(json_facts(inputs(case))) == before
    assert tuple(case.cas.store._db.iterdump()) == rows
    assert scope.limits is not inputs(case)["limits"]
    probe.retired()
    assert probe.events[-1] == ("entry", "auth")
    reads = probe.events.count(("cas-read", "enter"))
    assert build(case, checkpoint=probe.control)[0] == scope
    assert probe.scopes == 2 and probe.events.count(("cas-read", "enter")) > reads


@pytest.mark.parametrize("error_kind", ERROR_KINDS)
@pytest.mark.parametrize(
    "phase,channel",
    [
        ("entry", "auth"),
        ("pure-enter", "auth"),
        ("pure-exit", "auth"),
        ("pure-exit", "local"),
        ("baseline", "local"),
        ("reference", "local"),
        ("base-catalog", "local"),
        ("after-catalog", "local"),
        ("limits", "local"),
        ("_build", "auth"),
        ("parse_git_commit", "auth"),
        ("prepare_git_tree_diff", "auth"),
        ("snapshot_git_inventory_scope", "auth"),
        ("snapshot_git_inventory_scope", "local"),
    ],
)
def test_first_callback_and_boundary_error_identity_never_delivers_result(
    case, monkeypatch, phase, channel, error_kind
):
    probe = _Probe()
    _observe(monkeypatch, case, probe)
    marker = _error(error_kind)
    probe.failure_at, probe.failure = (phase, channel), marker
    delivered = None
    with pytest.raises(type(marker)) as caught:
        delivered = build(case, checkpoint=probe.control)
    assert caught.value is marker and delivered is None
    assert probe.control._segment is None
    callbacks = [event for event in probe.events if event[1] in {"local", "auth"}]
    assert callbacks[-1] == (phase, channel)
    if phase in {
        "entry",
        "pure-enter",
        "pure-exit",
        "baseline",
        "reference",
        "base-catalog",
        "after-catalog",
        "limits",
    }:
        assert ("_build", "enter") not in probe.events
        assert ("cas-read", "enter") not in probe.events
    if probe.retired is not None:
        probe.failure_at = None
        probe.phase = "retired"
        probe.retired()
        assert probe.events[-1] == ("retired", "auth")


@pytest.mark.parametrize("port", ["_snapshot", "_catalog", "_snapshot_model", "_build"])
@pytest.mark.parametrize("error_kind", ["os", "value", "kernel", "upstream", "nested-upstream"])
def test_noncallback_parser_errors_keep_original_mapping(case, monkeypatch, port, error_kind):
    marker = _error(error_kind)

    def fail(*args, **kwargs):
        raise marker

    monkeypatch.setattr(assembly, port, fail)
    expected = marker.error if isinstance(marker, UpstreamCheckpointError) else marker
    with pytest.raises(type(expected)) as caught:
        build(case, checkpoint=GitAuthenticationControl(lambda: None, lambda: None))
    assert caught.value is expected


@pytest.mark.parametrize("max_parents", [True, -1, 1.0, "3"])
def test_invalid_parent_limit_never_reaches_build_or_cas(case, monkeypatch, max_parents):
    probe = _Probe()
    _observe(monkeypatch, case, probe)
    with pytest.raises(KernelError) as caught:
        build(case, checkpoint=probe.control, max_parents=max_parents)
    assert caught.value.code == "git_inventory_limit_invalid"
    assert ("_build", "enter") not in probe.events
    assert probe.control._segment is None


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_unknown_callbacks_keep_exact_plain_trace(case, monkeypatch, kind):
    probe = _Probe()
    _observe(monkeypatch, case, probe)
    build(case, checkpoint=probe.authenticate)
    expected = probe.events[:]
    probe.events.clear()

    def forbidden_pure():
        pytest.fail("未知控制不可进入其自有 pure")

    def function():
        probe.authenticate()

    function.pure = forbidden_pure

    class Proxy:
        pure = staticmethod(forbidden_pure)

        def __call__(self):
            probe.authenticate()

    class Subclass(GitAuthenticationControl):
        pure = staticmethod(forbidden_pure)

    callback = {
        "function": function,
        "proxy": Proxy(),
        "subclass": Subclass(probe.local, probe.authenticate),
    }[kind]
    build(case, checkpoint=callback)
    assert probe.events == expected
    assert not any(channel == "local" for _phase, channel in probe.events)


@pytest.mark.parametrize("kind", ["object", "proxy", "subclass"])
def test_initial_full_check_and_exact_cas_rejection_precede_pure(case, monkeypatch, kind):
    probe = _Probe()
    _observe(monkeypatch, case, probe)

    class Proxy:
        def read(self, *args):
            pytest.fail("CAS 代理不得被调用")

    class Subclass(GitMaterialCAS):
        pass

    wrong = {"object": object(), "proxy": Proxy(), "subclass": Subclass(case.cas.store)}[kind]
    with pytest.raises(KernelError) as caught:
        build(case, target_cas=wrong, checkpoint=probe.control)
    assert caught.value.code == "git_inventory_materials_invalid"
    assert probe.events == [("entry", "auth")] and probe.scopes == 0


def test_real_foreign_task_keeps_full_leaves_without_rebinding_control(case, monkeypatch):
    async def parent():
        probe = _Probe()
        owner = asyncio.current_task()
        _observe(monkeypatch, case, probe)

        async def child():
            assert asyncio.current_task() is not owner
            return build(case, checkpoint=probe.control)

        scope, diff = await asyncio.create_task(child(), context=copy_context())
        assert probe.control._task is owner
        assert probe.scopes == 1
        assert not any(channel == "local" for _phase, channel in probe.events)
        assert canonical(json_facts(scope)) == canonical(json_facts(case.core.object_scope))
        assert diff.content.text == case.diff_text
        for phase in ("baseline", "reference", "base-catalog", "after-catalog", "limits"):
            assert (phase, "auth") in probe.events

    asyncio.run(parent())


def test_real_foreign_thread_keeps_full_leaves_without_rebinding_control(tmp_path, monkeypatch):
    probe = _Probe()
    original_owner = probe.control._task, probe.control._thread

    def worker():
        with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
            case = make_observed_case(GitMaterialCAS(store), tmp_path)[0]
            _observe(monkeypatch, case, probe)
            scope, diff = build(case, checkpoint=probe.control)
            assert canonical(json_facts(scope)) == canonical(json_facts(case.core.object_scope))
            assert diff.content.text == case.diff_text

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(copy_context().run, worker).result(timeout=15)
    assert (probe.control._task, probe.control._thread) == original_owner
    assert probe.scopes == 1
    assert not any(channel == "local" for _phase, channel in probe.events)
