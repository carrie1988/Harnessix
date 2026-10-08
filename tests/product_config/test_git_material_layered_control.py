"""材料控制的原Task／引用频检；真实CAS夹具，Owner及根观察替身不计SDK认证。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import git_checkpoint_materials as materials
from harnessix.product_config.git_delivery_plan_snapshot import _snapshot
from harnessix.product_config.git_delivery_process import GitDeliveryProcess, GitOperationBudget
from harnessix.product_config.git_parent_contracts import ProductGitDeliveryBaselineV2
from harnessix.product_config.git_process_host import GitProcessRuntimeHost
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.support.git_delivery_observed_core import make_observed_case


@pytest.fixture
def case(tmp_path, monkeypatch):
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state / "workspace-transactions") as store:
        cas = GitMaterialCAS(store)
        core = make_observed_case(cas, tmp_path)[0].core
        trace = []
        host = GitProcessRuntimeHost(object(), object(), object(), object())
        port = GitDeliveryProcess.__new__(GitDeliveryProcess)
        port._runtime_host, port._runner = host, object()
        port._state, port._closed, port._output_redaction = state, False, host.protection

        def owner_check(actual, root):
            assert actual is host and root == state
            trace.append("host")

        def root_check(source, root, checkpoint):
            assert source is core.baseline.source
            trace.append("root")
            checkpoint()
            return True

        monkeypatch.setattr(GitProcessRuntimeHost, "checkpoint", owner_check)
        monkeypatch.setattr(materials, "_root_binding_matches", root_check)
        yield SimpleNamespace(
            port=port,
            cas=cas,
            baseline=core.baseline,
            root=tmp_path,
            cancel=CancelToken(),
            budget=GitOperationBudget(60),
            trace=trace,
        )


def bind(case, checkpoint):
    return materials._material_control(
        case.port, case.root, lambda: case.baseline, case.cas, case.cancel, case.budget, checkpoint
    )


def original(case):
    return GitAuthenticationControl(
        lambda: case.trace.append("local"), lambda: case.trace.append("auth")
    )


async def test_same_owner_baseline_snapshot_is_pure_but_full_boundaries_keep_root_checks(case):
    parent = original(case)
    check = bind(case, parent)
    assert type(check) is GitAuthenticationControl and check._task is parent._task
    saved = _snapshot(case.baseline, ProductGitDeliveryBaselineV2, check)
    assert saved == case.baseline and saved is not case.baseline
    assert case.trace[:4] == case.trace[-4:] == ["auth", "host", "root", "auth"]
    assert set(case.trace[4:-4]) == {"local"} and len(case.trace[4:-4]) > 10
    assert parent._segment is None
    before = list(case.trace)
    check()
    assert case.trace == before + ["auth", "host", "root", "auth"]


async def test_collector_root_checks_follow_its_cloned_baseline_not_mutable_input(
    case, monkeypatch
):
    original_baseline = case.baseline
    cloned = []
    result = (object(), object())

    def root_check(source, root, checkpoint):
        expected = cloned[0] if cloned else original_baseline
        assert source is expected.source
        checkpoint()
        return True

    async def collect_base(port, root, baseline, cas, *args):
        assert baseline is not original_baseline and baseline == original_baseline
        cloned.append(baseline)
        object.__setattr__(original_baseline, "source", object())
        args[-2]()  # 同一完整控制点，不让原声明别名改写后继根绑定。
        return {baseline.head_oid: object()}

    monkeypatch.setattr(materials, "_root_binding_matches", root_check)
    monkeypatch.setattr(materials, "_collect_base", collect_base)
    monkeypatch.setattr(materials, "_collect_after", lambda *args: {})
    monkeypatch.setattr(materials, "build_product_git_checkpoint_scope", lambda *args, **kw: result)
    assert (
        await materials.collect_product_git_checkpoint_materials(
            case.port,
            case.root,
            original_baseline,
            case.cas,
            authorize=lambda *args: None,
            limits=GitTreeClosureLimits(1000, 65536, 1000, 64),
            max_parents=8,
            cancel=case.cancel,
            budget=case.budget,
            checkpoint=original(case),
        )
        is result
    )


@pytest.mark.parametrize("drift", ["port", "store", "runner", "state", "protection", "root"])
async def test_local_ref_drift_rejects_without_root_or_owner_io(case, drift):
    check = bind(case, original(case))
    with pytest.raises(KernelError) as caught:
        with check.pure() as leaf:
            case.trace.clear()
            if drift == "port":
                case.port._closed = True
            elif drift == "store":
                object.__setattr__(case.cas, "store", object())
            elif drift == "runner":
                case.port._runner = object()
            elif drift == "state":
                case.port._state = object()
            elif drift == "protection":
                case.port._output_redaction = object()
            else:
                case.cas.store._root = object()
            leaf()
    assert caught.value.code == "git_checkpoint_materials_invalid"
    assert case.trace == ["local"] and check._segment is None


@pytest.mark.parametrize("field", ["_local_check", "_authenticate", "_task", "_thread", "_origin"])
async def test_parent_control_replacement_cannot_invoke_a_new_local_callback(case, field):
    parent = original(case)
    check = bind(case, parent)
    invoked = []
    with pytest.raises(KernelError) as caught:
        with check.pure() as leaf:
            case.trace.clear()
            setattr(parent, field, lambda: invoked.append(True))
            leaf()
    assert caught.value.code == (
        "git_authentication_control_invalid"
        if field == "_origin"
        else "git_checkpoint_materials_invalid"
    )
    assert case.trace == [] and invoked == []


@pytest.mark.parametrize("fault", ["parent_dict", "parent_key", "closed_flag", "port_class"])
async def test_executable_local_shapes_are_rejected_before_their_callbacks(case, fault):
    parent = original(case)
    check = bind(case, parent)
    invoked = []

    def forbidden(*args):
        invoked.append(True)
        raise AssertionError("纯段局部检查不得执行替换回调")

    with pytest.raises(KernelError) as caught:
        with check.pure() as leaf:
            if fault == "parent_dict":

                class Attributes(dict):
                    copy = forbidden

                parent.__dict__ = Attributes(vars(parent))
            elif fault == "parent_key":

                class Name(str):
                    __eq__ = forbidden
                    __hash__ = str.__hash__

                parent.__dict__ = {
                    Name(name) if name == "_local_check" else name: value
                    for name, value in vars(parent).items()
                }
            elif fault == "closed_flag":

                class Flag:
                    __bool__ = forbidden

                case.port._closed = Flag()
            else:

                class Port(GitDeliveryProcess):
                    __getattribute__ = forbidden

                case.port.__class__ = Port
            leaf()
    assert caught.value.code == "git_checkpoint_materials_invalid" and invoked == []


@pytest.mark.parametrize("fault", ["cancel", "deadline"])
async def test_local_stop_precedes_upstream_ref_drift(case, fault):
    parent = original(case)
    check = bind(case, parent)
    with pytest.raises(TurnCancelled if fault == "cancel" else KernelError) as caught:
        with check.pure() as leaf:
            parent._local_check = lambda: pytest.fail("停止不得继续调用替换回调")
            case.budget._deadline = 0
            if fault == "cancel":
                case.cancel.cancel()
            leaf()
    if fault == "deadline":
        assert caught.value.code == "git_process_timeout"


@pytest.mark.parametrize("phase", ["local", "enter", "exit"])
@pytest.mark.parametrize("kind", ["timeout", "kernel", "upstream", "cancel", "os"])
async def test_first_callback_error_identity_and_failed_exit_never_deliver(case, phase, kind):
    marker = {
        "timeout": TimeoutError("upstream"),
        "kernel": KernelError("test_stop", "stop"),
        "upstream": UpstreamCheckpointError(ValueError("nested")),
        "cancel": asyncio.CancelledError(),
        "os": OSError("upstream"),
    }[kind]
    calls = []

    def full():
        calls.append("full")
        if phase == "enter" or (phase == "exit" and len(calls) >= 3):
            raise marker

    def local():
        if phase == "local":
            raise marker

    check = bind(case, GitAuthenticationControl(local, full))
    delivered = None
    with pytest.raises(type(marker)) as caught:
        delivered = _snapshot(case.baseline, ProductGitDeliveryBaselineV2, check)
    assert caught.value is marker and delivered is None and check._segment is None


async def test_foreign_parent_task_and_thread_do_not_gain_material_local_control(case):
    parent = original(case)

    async def foreign():
        check = bind(case, parent)
        assert type(check) is not GitAuthenticationControl
        check()

    await asyncio.create_task(foreign())
    with ThreadPoolExecutor(max_workers=1) as executor:
        await asyncio.wrap_future(executor.submit(asyncio.run, foreign()))
    assert "local" not in case.trace


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
async def test_unknown_controls_keep_full_root_trace_and_do_not_call_pure(case, kind):
    def callback():
        case.trace.append("auth")

    def forbidden():
        pytest.fail("不能将未知回调或子类视为原分层控制")

    callback.pure = forbidden

    class Proxy:
        __call__ = staticmethod(callback)
        pure = staticmethod(forbidden)

    class Subclass(GitAuthenticationControl):
        pure = staticmethod(forbidden)

    parent = {"function": callback, "proxy": Proxy(), "subclass": Subclass(callback, callback)}[
        kind
    ]
    check = bind(case, parent)
    assert type(check) is not GitAuthenticationControl
    check()
    assert case.trace == ["auth", "host", "root", "auth"]
