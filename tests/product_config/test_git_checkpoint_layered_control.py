"""准备控制闭包的分层与原Task归属负控；真实Owner/SDK另由集成测例验证。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.product_config import git_checkpoint_preparation as preparation
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_process import GitDeliveryProcess, GitOperationBudget
from harnessix.product_config.git_process_host import GitProcessRuntimeHost
from harnessix.trusted_actions.router import ActionPlanningContext, ResolvedAction


@pytest.fixture
def case(monkeypatch, tmp_path):
    # 只隔离昂贵认证的调用轨迹，不将占位资源宣称为真实产品授权。
    trace = []
    source = ["original"]
    authority_failure = []
    host = GitProcessRuntimeHost(object(), object(), object(), object())
    core = ProductGitDeliveryCoreStore.__new__(ProductGitDeliveryCoreStore)
    object.__setattr__(core, "store", object())
    port = GitDeliveryProcess.__new__(GitDeliveryProcess)
    port._runtime_host, port._runner, port._closed = host, object(), False
    planner = preparation.ProductGitCheckpointPreparer.__new__(
        preparation.ProductGitCheckpointPreparer
    )
    planner.session = SimpleNamespace(_runtime_owner_token=host.owner)
    planner.router, planner.reader, planner.ports = object(), object(), object()
    planner.core_store, planner.material_port = core, port
    context = ActionPlanningContext(
        tmp_path, object(), object(), checkpoint=lambda: trace.append("caller")
    )

    def digest():
        trace.append("source")
        return source[0]

    def authority():
        trace.append("authority")
        if authority_failure:
            raise authority_failure[0]

    def freeze(*args):
        assert args == (planner.session, planner.router, core.store, planner.ports, planner.reader)
        return authority

    def verify(actual, original_host, owner, actual_context):
        assert (actual, original_host, owner, actual_context) == (
            planner,
            host,
            host.owner,
            context,
        )
        trace.append("host")

    monkeypatch.setattr(preparation, "git_checkpoint_preparation_implementation_digest", digest)
    monkeypatch.setattr(preparation, "require_git_user_authority", freeze)
    monkeypatch.setattr(preparation, "_verify_host", verify)
    return SimpleNamespace(
        planner=planner,
        context=context,
        trace=trace,
        source=source,
        authority_failure=authority_failure,
        cancel=CancelToken(),
        budget=GitOperationBudget(60),
        failures=[],
    )


def controls(case):
    return preparation._preparation_control(
        case.planner, case.context, case.cancel, case.budget, case.failures
    )


async def test_pure_leaves_skip_external_auth_but_boundaries_keep_original_order(case):
    local, full = controls(case)
    case.trace.clear()
    control = GitAuthenticationControl(local, full)
    with control.pure() as check:
        for _ in range(100):
            check()
        assert case.trace == ["caller", "authority", "source", "host"]
    assert case.trace == ["caller", "authority", "source", "host"] * 2
    assert not case.failures


@pytest.mark.parametrize("drift", ["store", "port", "owner", "runner", "key", "value"])
async def test_local_rejects_original_resource_drift_without_external_calls(case, drift):
    local, _ = controls(case)
    case.trace.clear()
    planner = case.planner
    if drift == "store":
        object.__setattr__(planner.core_store, "store", object())
    elif drift == "port":
        planner.material_port._closed = True
    elif drift == "owner":
        planner.session._runtime_owner_token = object()
    elif drift == "runner":
        planner.material_port._runner = object()
    elif drift == "key":
        planner.added = None
    else:
        # 等值但不同引用也不能替换入口的原资源。
        planner.session = SimpleNamespace(**vars(planner.session))
    with pytest.raises(KernelError) as caught:
        local()
    assert caught.value.code == "git_checkpoint_preparation_invalid"
    assert case.failures == [caught.value] and case.trace == []


@pytest.mark.parametrize("field", ["core_store", "material_port", "session"])
async def test_local_rejects_proxy_before_dereferencing_its_properties(case, field):
    local, _ = controls(case)
    calls = []

    class Proxy:
        def __getattribute__(self, name):
            calls.append(name)
            raise AssertionError("替换资源不得执行属性回调")

    setattr(case.planner, field, Proxy())
    with pytest.raises(KernelError) as caught:
        local()
    assert caught.value.code == "git_checkpoint_preparation_invalid"
    assert calls == []


@pytest.mark.parametrize("field", ["core_store", "material_port", "session", "reader"])
async def test_local_missing_planner_field_is_canonical_failure(case, field):
    local, _ = controls(case)
    delattr(case.planner, field)
    with pytest.raises(KernelError) as caught:
        local()
    assert caught.value.code == "git_checkpoint_preparation_invalid"


async def test_sparse_planner_dictionary_rejects_keys_before_copy_can_execute_equality(case):
    local, _ = controls(case)
    calls, armed = [], [False]

    class Name(str):
        def __hash__(self):
            return 17

        def __eq__(self, other):
            if armed[0]:
                calls.append("equality")
                raise AssertionError("planner dictionary copy must not invoke key equality")
            return str.__eq__(self, other)

    attributes = case.planner.__dict__.copy()
    attributes.update({Name("bad-one"): None, Name("bad-two"): None})
    for index in range(100):
        attributes[f"filler-{index}"] = None
    for index in range(100):
        del attributes[f"filler-{index}"]
    case.planner.__dict__ = attributes
    armed[0] = True
    case.trace.clear()
    with pytest.raises(KernelError) as failure:
        local()
    assert failure.value.code == "git_checkpoint_preparation_invalid"
    assert calls == [] and case.trace == [] and case.failures == [failure.value]


@pytest.mark.parametrize(
    "resource,field",
    [
        ("core_store", "store"),
        ("material_port", "_closed"),
        ("material_port", "_runtime_host"),
        ("material_port", "_runner"),
        ("session", "_runtime_owner_token"),
        ("host", "owner"),
        ("host", "supervisor"),
        ("host", "plans"),
        ("host", "protection"),
    ],
)
async def test_local_missing_original_resource_member_is_canonical_failure(case, resource, field):
    local, _ = controls(case)
    target = (
        case.planner.material_port._runtime_host
        if resource == "host"
        else getattr(case.planner, resource)
    )
    object.__delattr__(target, field)
    with pytest.raises(KernelError) as caught:
        local()
    assert caught.value.code == "git_checkpoint_preparation_invalid"
    assert case.failures == [caught.value]


@pytest.mark.parametrize(
    "fault", ["closed_flag", "dict_subclass", "key_subclass", "port_class", "host_class"]
)
async def test_local_rejects_executable_memory_shapes_before_callbacks(case, fault):
    local, _ = controls(case)
    invoked = []

    def forbidden(*args):
        invoked.append(True)
        raise AssertionError("局部内存检查不得执行外部回调")

    if fault == "closed_flag":

        class Flag:
            __bool__ = forbidden

        case.planner.material_port._closed = Flag()
    elif fault == "dict_subclass":

        class Attributes(dict):
            copy = forbidden

        case.planner.__dict__ = Attributes(vars(case.planner))
    elif fault == "key_subclass":

        class Name(str):
            __eq__ = forbidden
            __hash__ = str.__hash__

        case.planner.__dict__ = {
            Name(name) if name == "session" else name: value
            for name, value in vars(case.planner).items()
        }
    elif fault == "port_class":

        class Port(GitDeliveryProcess):
            __getattribute__ = forbidden

        case.planner.material_port.__class__ = Port
    else:

        class Host(GitProcessRuntimeHost):
            __slots__ = ()
            __getattribute__ = forbidden

        object.__setattr__(case.planner.material_port._runtime_host, "__class__", Host)
    with pytest.raises(KernelError) as caught:
        local()
    assert caught.value.code == "git_checkpoint_preparation_invalid"
    assert invoked == []


@pytest.mark.parametrize("fault", ["cancel", "deadline"])
async def test_local_stop_precedes_resource_drift(case, fault):
    local, _ = controls(case)
    case.planner.material_port._closed = True
    case.budget._deadline = 0
    if fault == "cancel":
        case.cancel.cancel()
    with pytest.raises(TurnCancelled if fault == "cancel" else KernelError) as caught:
        local()
    if fault == "deadline":
        assert caught.value.code == "git_process_timeout"
    assert case.failures == [caught.value]


async def test_full_failure_order_and_identity_remain_before_source_and_resource_drift(case):
    _, full = controls(case)
    marker = TimeoutError("original caller failure")
    case.authority_failure.append(marker)
    case.source[0] = "changed"
    case.planner.material_port._closed = True
    case.trace.clear()
    with pytest.raises(TimeoutError) as caught:
        full()
    assert caught.value is marker
    assert case.failures == [marker] and case.trace == ["caller", "authority"]


async def test_exit_full_auth_rejects_source_drift_and_never_returns_result(case):
    local, full = controls(case)
    control = GitAuthenticationControl(local, full)
    delivered = None

    def compute():
        with control.pure() as check:
            case.source[0] = "changed"
            check()
        return 42

    with pytest.raises(KernelError) as caught:
        delivered = compute()
    assert caught.value.code == "git_checkpoint_preparation_invalid" and delivered is None


async def test_managed_child_can_use_pure_without_recapturing_parent_resources(case, monkeypatch):
    parent = asyncio.current_task()
    result = ResolvedAction(())

    async def prepare(*args):
        assert asyncio.current_task() is not parent
        control, native_observer = args[-2:]
        assert callable(native_observer) and native_observer is not control
        assert type(control) is GitAuthenticationControl
        assert control._task is asyncio.current_task()
        case.trace.clear()
        with control.pure() as check:
            for _ in range(10):
                check()
        assert case.trace == ["caller", "authority", "source", "host"] * 2
        return result

    monkeypatch.setattr(preparation, "_prepare", prepare)
    actual = await preparation._prepare_entry(
        case.planner, None, None, case.context, None, None, None, case.cancel
    )
    assert actual is result


async def test_preparer_subclass_keeps_original_full_control(case, monkeypatch):
    class Preparer(preparation.ProductGitCheckpointPreparer):
        pass

    case.planner.__class__ = Preparer
    result = ResolvedAction(())

    async def prepare(*args):
        control, native_observer = args[-2:]
        assert native_observer is None
        assert type(control) is not GitAuthenticationControl
        case.trace.clear()
        control()
        assert case.trace == ["caller", "authority", "source", "host"]
        return result

    monkeypatch.setattr(preparation, "_prepare", prepare)
    assert (
        await preparation._prepare_entry(
            case.planner, None, None, case.context, None, None, None, case.cancel
        )
        is result
    )


@pytest.mark.parametrize("fault", ["resource", "timeout"])
async def test_child_uses_parent_frozen_refs_and_preserves_callback_timeout(
    case, monkeypatch, fault
):
    marker = TimeoutError("same upstream timeout")

    async def prepare(*args):
        control, native_observer = args[-2:]
        assert callable(native_observer) and native_observer is not control
        if fault == "resource":
            case.planner.reader = object()
        else:
            case.authority_failure.append(marker)
        with control.pure():
            pytest.fail("中途替换或上游失败不得进入计算段")

    monkeypatch.setattr(preparation, "_prepare", prepare)
    with pytest.raises(KernelError if fault == "resource" else TimeoutError) as caught:
        await preparation._prepare_entry(
            case.planner, None, None, case.context, None, None, None, case.cancel
        )
    if fault == "resource":
        assert caught.value.code == "git_checkpoint_preparation_invalid"
    else:
        assert caught.value is marker


async def test_local_preserves_new_parent_task_cancellation(case):
    local, _ = controls(case)
    parent = asyncio.current_task()
    parent.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            local()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
    finally:
        parent.uncancel()
