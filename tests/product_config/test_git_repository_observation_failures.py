"""只读仓库观察的宿主、批准、期限、取消、物理身份与强失败边界。"""

from __future__ import annotations

import asyncio
import os

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_repository_observation import (
    GitRepositoryReadAuthorization,
    observe_product_git_repository,
)
from harnessix.product_config.state_owner import state_owner_anchor
from tests.product_config.git_repository_observation_support import (
    _HOST_FAILURES,
    _assert_no_new_process,
    _assert_settled,
    _AuthorizedRead,
    _invalidate_host,
    _observe,
    _source_snapshot,
    original,
)
from tests.product_config.git_repository_observation_support import (
    make_process as make_process,
)
from tests.product_config.git_repository_observation_support import (
    shared_repository as shared_repository,
)

pytestmark = pytest.mark.skipif(os.name not in {"posix", "nt"}, reason="需要原生 Process Owner")


@pytest.mark.parametrize("host_value", [None, object()], ids=["missing", "wrong-type"])
async def test_missing_original_host_is_fixed_mismatch_before_authorize(make_process, host_value):
    case = make_process()
    case.port._runtime_host = host_value
    calls = []

    async def authorize(prepared):
        calls.append(prepared)
        raise AssertionError("缺少原宿主时不得请求批准")

    with pytest.raises(KernelError) as failure:
        await observe_product_git_repository(
            case.port,
            case.workspace,
            "d" * 64,
            authorize=authorize,
            cancel=CancelToken(),
            budget=GitOperationBudget(20),
            checkpoint=lambda: None,
        )
    assert failure.value.code == "git_process_runtime_mismatch"
    assert not calls
    original._assert_not_started(case)


async def test_closed_port_stops_before_authorize_without_closing_host(shared_repository):
    repository = shared_repository
    await repository.case.port.aclose()
    with pytest.raises(KernelError) as failure:
        await _observe(repository)
    assert failure.value.code == "git_process_closed"
    assert not repository.authorized
    _assert_no_new_process(repository)
    repository.host.checkpoint(repository.case.state)


@pytest.mark.parametrize(
    "approval", ["missing", "rejected", "deny", "another-plan", "another-command"]
)
async def test_unapproved_reads_never_save_plan_or_start_owner(shared_repository, approval):
    repository = shared_repository
    before = _source_snapshot(repository.case.workspace)

    async def authorize(prepared):
        case = repository.case
        if approval == "another-command":
            prepared = case.port.prepare(case.workspace, ("version",), budget=prepared.budget)
        plan = original._plan(
            case,
            prepared,
            decision=original.PolicyDecisionKind.DENY
            if approval == "deny"
            else original.PolicyDecisionKind.REQUIRE_APPROVAL,
        )
        accepted = None if approval == "missing" else original._checkpoint(plan)
        if approval == "rejected":
            accepted = original._checkpoint(plan, original.ApprovalOutcome.REJECTED)
        elif approval == "another-plan":
            accepted = original._checkpoint(original._plan(case, prepared))
        repository.authorized.append(_AuthorizedRead(prepared, plan, accepted))
        return GitRepositoryReadAuthorization(plan, accepted)

    with pytest.raises(KernelError) as failure:
        await _observe(repository, authorize=authorize)
    assert failure.value.code == (
        "git_process_plan_mismatch" if approval == "another-command" else "approval_required"
    )
    assert len(repository.authorized) == 1
    _assert_no_new_process(repository)
    assert _source_snapshot(repository.case.workspace) == before


@pytest.mark.parametrize("stop", ["token", "timeout", "task"])
@pytest.mark.parametrize("phase", ["initial", "between"])
async def test_waiting_authorize_is_cancelled_and_drained_without_start(
    shared_repository, stop, phase
):
    repository = shared_repository
    entered, drained = asyncio.Event(), asyncio.Event()
    cancel = CancelToken()
    budget = GitOperationBudget((0.35 if phase == "initial" else 2) if stop == "timeout" else 20)
    calls = []

    async def authorize(prepared):
        assert prepared.budget is budget
        calls.append(prepared)
        if phase == "between" and len(calls) == 1:
            record = repository.approve(prepared)
            return GitRepositoryReadAuthorization(record.plan, record.approval)
        entered.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("未完成的批准不应恢复执行")
        finally:
            drained.set()

    task = asyncio.create_task(
        _observe(repository, authorize=authorize, cancel=cancel, budget=budget)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if stop == "token":
            cancel.cancel()
        elif stop == "task":
            task.cancel()
        error_type = (
            KernelError
            if stop == "timeout"
            else TurnCancelled
            if stop == "token"
            else asyncio.CancelledError
        )
        with pytest.raises(error_type) as failure:
            await asyncio.wait_for(asyncio.shield(task), 5)
        if stop == "timeout":
            assert failure.value.code == "git_process_timeout"
        count = 0 if phase == "initial" else 1
        assert drained.is_set() and len(calls) == count + 1
        _assert_no_new_process(repository, count)
        _assert_settled(repository)
        repository.host.checkpoint(repository.case.state)
    finally:
        cancel.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("stop", ["token", "budget"])
async def test_authorize_return_cannot_reset_cancel_or_operation_budget(shared_repository, stop):
    repository = shared_repository
    cancel = CancelToken()
    budget = GitOperationBudget(0.35 if stop == "budget" else 20)

    async def authorize(prepared):
        assert prepared.budget is budget
        record = repository.approve(prepared)
        if stop == "token":
            cancel.cancel()
        else:
            # 同步审批计算消耗的时间也属于原绝对期限，不能靠 await 超时单独证明。
            await asyncio.to_thread(original.time.sleep, budget.remaining() + 0.05)
        return GitRepositoryReadAuthorization(record.plan, record.approval)

    with pytest.raises(KernelError if stop == "budget" else TurnCancelled) as failure:
        await _observe(repository, authorize=authorize, cancel=cancel, budget=budget)
    if stop == "budget":
        assert failure.value.code == "git_process_timeout"
    _assert_no_new_process(repository)


@pytest.mark.parametrize("stop", ["cancel", "expired-budget"])
async def test_initial_cancel_and_total_budget_reject_before_authorize(shared_repository, stop):
    repository = shared_repository
    cancel = CancelToken()
    budget = GitOperationBudget(0.05 if stop == "expired-budget" else 20)
    if stop == "cancel":
        cancel.cancel()
    else:
        await asyncio.sleep(0.06)
    with pytest.raises(TurnCancelled if stop == "cancel" else KernelError) as failure:
        await _observe(repository, cancel=cancel, budget=budget)
    if stop == "expired-budget":
        assert failure.value.code == "git_process_timeout"
    assert not repository.authorized
    _assert_no_new_process(repository)


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    [(fault, code) for fault, code in _HOST_FAILURES if fault != "host-replaced"],
)
async def test_invalid_initial_host_cannot_request_approval_or_create_private_fallback(
    shared_repository, fault: str, expected_code: str
):
    repository = shared_repository
    await _invalidate_host(repository, fault)
    with pytest.raises(KernelError) as failure:
        await _observe(repository)
    assert failure.value.code == expected_code
    assert not repository.authorized
    _assert_no_new_process(repository)


@pytest.mark.parametrize(("fault", "expected_code"), _HOST_FAILURES)
async def test_original_host_invalidated_between_commands_prevents_next_start(
    shared_repository, fault: str, expected_code: str
):
    repository = shared_repository
    before = _source_snapshot(repository.case.workspace)

    async def authorize(prepared):
        record = repository.approve(prepared)
        if len(repository.authorized) == 2:
            _assert_no_new_process(repository, 1)
            await _invalidate_host(repository, fault)
        return GitRepositoryReadAuthorization(record.plan, record.approval)

    with pytest.raises(KernelError) as failure:
        await _observe(repository, authorize=authorize)
    assert failure.value.code == expected_code
    assert len(repository.authorized) == 2
    _assert_no_new_process(repository, 1)
    # 已完成的第一条读取保留真实认证证据；第二条批准材料不能生成 Plan/Lease。
    first = repository.authorized[0]
    lease = original._lease(repository.case, first.prepared)
    assert lease.state == "exited" and lease.returncode == 0
    assert original._receipt(repository.case, first.prepared).state == "exited"
    assert _source_snapshot(repository.case.workspace) == before


@pytest.mark.parametrize("phase", ["entry", "between", "final"])
async def test_caller_checkpoint_is_required_through_final_return(shared_repository, phase):
    repository = shared_repository
    failure = KernelError("observation_checkpoint_rejected", "调用方状态检查拒绝")
    before = _source_snapshot(repository.case.workspace)

    def checkpoint():
        started = len(repository.host.supervisor._handles)
        final = bool(
            repository.authorized
            and repository.authorized[-1].prepared.command.arguments == ("version",)
            and started == len(repository.authorized)
        )
        if (
            phase == "entry"
            or (phase == "between" and started == 1)
            or (phase == "final" and final)
        ):
            raise failure

    with pytest.raises(KernelError) as observed:
        await _observe(repository, checkpoint=checkpoint)
    assert observed.value is failure
    assert repository.checkpoints
    if phase != "final":
        _assert_no_new_process(repository, 0 if phase == "entry" else 1)
    _assert_settled(repository)
    assert _source_snapshot(repository.case.workspace) == before


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    [
        ("owner", "product_state_owner_invalid"),
        ("closed-plans", "git_process_runtime_mismatch"),
        ("closed-scope", "trusted_action_secret_unavailable"),
        ("restore-pending", "product_state_restore_pending"),
    ],
)
async def test_original_host_is_rechecked_after_last_settled_read(
    shared_repository, fault: str, expected_code: str
):
    repository = shared_repository
    changed = False

    def checkpoint():
        nonlocal changed
        if changed or not repository.authorized:
            return
        prepared = repository.authorized[-1].prepared
        handle = repository.host.supervisor._handles.get(prepared.spec.process_id)
        if (
            prepared.command.arguments != ("version",)
            or handle is None
            or handle.lease.state != "exited"
        ):
            return
        changed = True
        if fault == "owner":
            repository.host.owner._active = False
        elif fault == "closed-plans":
            repository.host.plans.close()
        elif fault == "closed-scope":
            repository.host.protection.close()
        else:
            (state_owner_anchor(repository.case.state) / "restore-active.json").write_text("{}")

    with pytest.raises(KernelError) as failure:
        await _observe(repository, checkpoint=checkpoint)
    assert failure.value.code == expected_code
    assert changed and repository.authorized[-1].prepared.command.arguments == ("version",)
    _assert_no_new_process(repository, len(repository.authorized))
    for record in repository.authorized:
        assert original._lease(repository.case, record.prepared).state == "exited"
        assert original._receipt(repository.case, record.prepared).state == "exited"


@pytest.mark.parametrize("phase", ["authorize", "final"])
async def test_port_close_inside_observation_cannot_start_later_reads(shared_repository, phase):
    repository = shared_repository

    async def authorize(prepared):
        record = repository.approve(prepared)
        if (phase == "authorize" and len(repository.authorized) == 2) or (
            phase == "final" and prepared.command.arguments == ("version",)
        ):
            await repository.case.port.aclose()
        return GitRepositoryReadAuthorization(record.plan, record.approval)

    with pytest.raises(KernelError) as failure:
        await _observe(repository, authorize=authorize)
    assert failure.value.code == "git_process_closed"
    count = len(repository.authorized) - 1
    _assert_no_new_process(repository, count)
    _assert_settled(repository, count)
    repository.host.checkpoint(repository.case.state)


@pytest.mark.parametrize("fault", ["root", "common", "alternates"])
async def test_final_repository_physical_identity_check_rejects_replacement(
    shared_repository, fault: str
):
    repository = shared_repository
    changed = False

    def checkpoint():
        nonlocal changed
        if changed or not repository.authorized:
            return
        prepared = repository.authorized[-1].prepared
        handle = repository.host.supervisor._handles.get(prepared.spec.process_id)
        if (
            prepared.command.arguments != ("version",)
            or handle is None
            or handle.lease.state != "exited"
        ):
            return
        changed = True
        root = repository.case.workspace
        if fault == "alternates":
            (root / ".git/objects/info/alternates").write_bytes(b"")
        else:
            original_path = root if fault == "root" else root / ".git"
            original_path.rename(original_path.with_name(original_path.name + "-replaced"))
            original_path.mkdir()

    with pytest.raises(KernelError) as failure:
        await _observe(repository, checkpoint=checkpoint)
    assert failure.value.code == "git_repository_changed"
    assert changed and repository.authorized[-1].prepared.command.arguments == ("version",)
    _assert_settled(repository)


@pytest.mark.parametrize("shared_repository", ["protected-root"], indirect=True)
async def test_real_owner_output_protection_failure_is_not_rewritten(shared_repository):
    repository = shared_repository
    before = _source_snapshot(repository.case.workspace)
    with pytest.raises(KernelError) as failure:
        await _observe(repository)
    assert failure.value.code == "git_process_output_changed"
    assert len(repository.authorized) == 1
    _assert_no_new_process(repository, 1)
    record = repository.authorized[0]
    lease = original._lease(repository.case, record.prepared)
    receipt = original._receipt(repository.case, record.prepared)
    assert lease.state == receipt.state == "exited"
    assert lease.plan_id == record.plan.plan_id
    assert receipt.raw_stdout.sha256 != lease.stdout.sha256
    assert _source_snapshot(repository.case.workspace) == before


@pytest.mark.parametrize(
    "code",
    [
        "git_process_unknown",
        "git_material_effect_unknown",
        "git_material_stage_changed",
        "process_owner_receipt_invalid",
        "process_output_corrupt",
        "process_control_lost",
        "process_owner_token_invalid",
    ],
)
@pytest.mark.parametrize("phase", ["authorize", "checkpoint"])
async def test_original_strong_failure_preserves_object_and_cause(
    shared_repository, code: str, phase: str
):
    repository = shared_repository
    cause = ExceptionGroup("原失败原因", [OSError("原状态无法验真"), ValueError("原回执无效")])
    original_failure = KernelError(code, "原调用方拒绝继续读取")
    calls = []

    async def authorize(prepared):
        calls.append(prepared)
        raise original_failure from cause

    def checkpoint():
        if repository.host.supervisor._handles:
            raise original_failure from cause

    with pytest.raises(KernelError) as observed:
        await _observe(
            repository,
            authorize=authorize if phase == "authorize" else None,
            checkpoint=checkpoint if phase == "checkpoint" else None,
        )
    assert observed.value is original_failure
    assert observed.value.__cause__ is cause
    assert len(calls) == (1 if phase == "authorize" else 0)
    _assert_no_new_process(repository, 0 if phase == "authorize" else 1)
    _assert_settled(repository)


@pytest.mark.parametrize("phase", ["initial", "final"])
async def test_parent_task_cancel_in_cpu_checkpoint_stops_before_prepare_or_return(
    shared_repository, phase: str
):
    repository = shared_repository
    before = _source_snapshot(repository.case.workspace)
    cancellation_requested = False

    def checkpoint():
        nonlocal cancellation_requested
        if cancellation_requested:
            return
        final = bool(
            repository.authorized
            and repository.authorized[-1].prepared.command.arguments == ("version",)
            and len(repository.host.supervisor._handles) == len(repository.authorized)
            and repository.host.supervisor._handles[
                repository.authorized[-1].prepared.spec.process_id
            ].lease.state
            == "exited"
        )
        if phase == "initial" or final:
            parent = asyncio.current_task()
            assert parent is not None
            cancellation_requested = True
            parent.cancel()

    async def observing():
        result = await _observe(repository, checkpoint=checkpoint)
        raise AssertionError(f"父 Task 取消后不应返回仓库绑定：{result.digest}")

    task = asyncio.create_task(observing())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancellation_requested and task.cancelled()
    if phase == "initial":
        assert not repository.authorized
        _assert_no_new_process(repository)
    else:
        assert repository.authorized[-1].prepared.command.arguments == ("version",)
    _assert_settled(repository)
    assert _source_snapshot(repository.case.workspace) == before
    repository.host.checkpoint(repository.case.state)


async def test_preexisting_parent_task_cancel_is_delivered_before_observation_checkpoint(
    shared_repository,
):
    repository = shared_repository
    before = _source_snapshot(repository.case.workspace)
    entered = False

    async def observing():
        nonlocal entered
        parent = asyncio.current_task()
        assert parent is not None
        parent.cancel()
        entered = True
        # 取消已被请求但还未到 await；入口应先交付，不能先 prepare 或生成计划。
        result = await _observe(repository)
        raise AssertionError(f"既有父取消后不应返回仓库绑定：{result.digest}")

    task = asyncio.create_task(observing())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert entered and task.cancelled()
    assert not repository.checkpoints and not repository.authorized
    _assert_no_new_process(repository)
    assert _source_snapshot(repository.case.workspace) == before
    repository.host.checkpoint(repository.case.state)


async def test_parent_cpu_cancel_cannot_replace_original_strong_checkpoint_failure(
    shared_repository,
):
    repository = shared_repository
    cause = ExceptionGroup("原强失败原因", [OSError("原控制状态无法验真")])
    original_failure = KernelError("git_process_unknown", "原状态检查拒绝继续执行")

    def checkpoint():
        parent = asyncio.current_task()
        assert parent is not None
        parent.cancel()
        raise original_failure from cause

    task = asyncio.create_task(_observe(repository, checkpoint=checkpoint))
    with pytest.raises(KernelError) as observed:
        await task
    assert observed.value is original_failure
    assert observed.value.__cause__ is cause
    assert not repository.authorized
    _assert_no_new_process(repository)
