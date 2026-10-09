"""窄切SHA拒绝：真实产品路径与审批、扩展、执行、取消负控。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TurnStatus,
)
from harnessix.delivery.contracts import WorkspaceFileVersion
from harnessix.delivery.trusted_action import (
    WorkspacePatchActionExecutor,
    WorkspacePatchTransactionPlanner,
    _validate_mutation,
)
from harnessix.delivery.workspace_patch_errors import (
    WorkspacePatchPreconditionError,
    is_workspace_patch_sha_mismatch,
)
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config.workspace_patch_review import WorkspacePatchReviewProvider
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, build_trusted_tool_binding
from harnessix.trusted_actions.preparation_rejection import workspace_patch_preparation_rejection
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step, _approval, _proposal
from tests.product_config.test_product_patch_rollback import product, result
from tests.trusted_actions.test_agent_gateway import (
    FakeExecutor,
    agent_state,
    build_gateway,
    descriptor,
    runtime_context,
)

CODE = "workspace_patch_precondition_failed"
MESSAGE = (
    "expected_sha256不匹配；请重新调用目标文件的read_file，"
    "仅使用digest_status=complete时的content_sha256，禁止猜测摘要"
)


def bad_proposal(operation="replace"):
    proposal = _proposal()
    return proposal.model_copy(
        update={
            "files": tuple(
                item.model_copy(update={"expected_sha256": "0" * 64})
                if item.operation == operation
                else item
                for item in proposal.files
            )
        }
    )


async def patch_turn(runtime, provider, root, proposal):
    provider.steps = (_action_step(proposal), answer("已处理"))
    thread = await runtime.create_thread(str(root))
    turn = await runtime.run_turn(thread.thread_id, "修改文件", request_id="patch")
    call = next(item.content for item in turn.items if isinstance(item.content, ToolCallContent))
    return thread, turn, call


def assert_unchanged(root):
    assert (root / "src/modified.py").read_bytes() == b"old\n"
    assert (root / "tests/deleted.txt").read_bytes() == b"remove\n"
    assert not (root / "src/新增.py").exists()


@pytest.mark.parametrize("operation", ["replace", "delete"])
@pytest.mark.parametrize("tampered", [False, True])
async def test_bad_sha_fails_without_executor_or_transaction_and_cannot_be_approved(
    tmp_path, monkeypatch, operation, tampered
):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):

        async def forbidden_execute(*_args):
            pytest.fail("错误SHA不得启动Executor")

        def forbidden_save(*_args):
            pytest.fail("错误SHA不得保存事务")

        monkeypatch.setattr(WorkspacePatchActionExecutor, "execute", forbidden_execute)
        monkeypatch.setattr(transactions, "save", forbidden_save)
        if tampered:

            def tampered_validation(*args):
                try:
                    _validate_mutation(*args)
                except WorkspacePatchPreconditionError as error:
                    error.code = "internal-code-canary"
                    error.message = "internal-message-canary"
                    raise

            monkeypatch.setattr(
                "harnessix.delivery.trusted_action._validate_mutation", tampered_validation
            )
        thread, turn, call = await patch_turn(runtime, provider, root, bad_proposal(operation))
        output = result(turn)
        assert turn.status is TurnStatus.COMPLETED
        assert output.outcome == "failed" and output.trusted_action is None
        assert output.error.code == CODE and output.error.message == MESSAGE
        assert not output.error.retryable
        assert not any(
            isinstance(item.content, TrustedActionApprovalRequestContent) for item in turn.items
        )
        plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
        assert router.status(plan_id).state == "denied"
        decision = router.approval(plan_id).decision
        assert (decision.outcome, decision.actor, decision.reason) == (
            ApprovalOutcome.REJECTED,
            "system.validation",
            CODE,
        )
        assert all(event.executor_id is None for event in router.events(plan_id))
        with pytest.raises(KernelError) as missing:
            transactions.load(plan_id)
        assert missing.value.code == "delivery_transaction_not_found"
        with pytest.raises(KernelError) as conflict:
            router.decide(plan_id, ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="user"))
        assert conflict.value.code == "approval_conflict"
        assert router.status(plan_id).state == "denied"
        assert_unchanged(root)


class DerivedPreconditionError(WorkspacePatchPreconditionError):
    pass


@pytest.mark.parametrize("origin", [None, True, object()], ids=["none", "boolean", "foreign"])
def test_direct_typed_error_has_no_original_sha_identity(origin):
    error = WorkspacePatchPreconditionError()
    assert not is_workspace_patch_sha_mismatch(error)
    error._sha_origin = origin
    assert not is_workspace_patch_sha_mismatch(error)
    assert error.review_plan_id is None


@pytest.mark.parametrize(
    "kind", ["same-code", "subclass", "review-runtime", "artifact-typed", "typed-prepare"]
)
async def test_non_sha_review_errors_remain_unknown(tmp_path, monkeypatch, kind):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, artifacts):
        error = (
            DerivedPreconditionError()
            if kind == "subclass"
            else WorkspacePatchPreconditionError()
            if kind in {"artifact-typed", "typed-prepare"}
            else RuntimeError("internal-path-canary")
            if kind == "review-runtime"
            else KernelError(CODE, "internal-path-canary", retryable=True)
        )
        if kind == "artifact-typed":

            async def fail_publication(*_args, **_kwargs):
                raise error

            monkeypatch.setattr(artifacts, "publish_action_review", fail_publication)
        else:

            def fail_prepare(*_args, **_kwargs):
                raise error

            monkeypatch.setattr(WorkspacePatchTransactionPlanner, "prepare", fail_prepare)
        thread, turn, call = await patch_turn(runtime, provider, root, _proposal())
        plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
        assert turn.status is TurnStatus.INTERRUPTED and result(turn).outcome == "unknown"
        assert router.status(plan_id).state == "pending_approval"
        assert router.approval(plan_id) is None
        assert "internal-path-canary" not in turn.model_dump_json()
        if kind in {"artifact-typed", "typed-prepare"}:
            assert error.review_plan_id is None
        if kind == "artifact-typed":
            assert transactions.load(plan_id).state == "prepared"
        assert_unchanged(root)


@pytest.mark.parametrize("kind", ["same-code", "typed", "forged-review-origin"])
async def test_replacement_callback_cannot_claim_builtin_sha_rejection(tmp_path, kind):
    async with product(tmp_path) as (root, runtime, provider, router, _, composition, _):

        class ExtensionReview:
            async def review(self, route, *_args):
                error = (
                    KernelError(CODE, "callback-canary")
                    if kind == "same-code"
                    else (WorkspacePatchPreconditionError())
                )
                if kind == "forged-review-origin":
                    error.review_plan_id = route.plan.execution.plan_id
                raise error

        composition.gateway._state.reviews["apply_patch_batch"] = ExtensionReview()
        thread, turn, call = await patch_turn(runtime, provider, root, _proposal())
        plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
        assert turn.status is TurnStatus.INTERRUPTED and result(turn).outcome == "unknown"
        assert router.status(plan_id).state == "pending_approval"
        assert router.approval(plan_id) is None
        assert "callback-canary" not in turn.model_dump_json()
        assert_unchanged(root)


@pytest.mark.parametrize("source", ["builtin", "custom", "mcp", "skill", "hook"])
@pytest.mark.parametrize("kind", ["same-code", "typed"])
async def test_other_tool_and_extension_review_errors_are_not_downgraded(tmp_path, source, kind):
    root = tmp_path / "workspace"
    root.mkdir()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    original, router, plans, audit = build_gateway(root, executor)
    original.close()
    tool = descriptor()
    definition = router._definition("builtin", "harnessix.product", tool.name)
    source_id = "harnessix.product" if source == "builtin" else "fixture.extension"
    if source != "builtin":
        fields = definition.binding.model_dump(exclude={"spec_version", "binding_digest"})
        fields.update(source=source, source_id=source_id)
        router.register(replace(definition, binding=build_trusted_tool_binding(**fields)))

    class FaultReview:
        async def review(self, route, *_args):
            error = (
                KernelError(CODE, "internal-canary")
                if kind == "same-code"
                else (WorkspacePatchPreconditionError())
            )
            if kind == "typed":
                error.review_plan_id = route.plan.execution.plan_id
            raise error

    gateway = RouterBackedAgentActionGateway(
        router,
        (tool,),
        lambda *_: runtime_context(root),
        source=source,
        source_id=source_id,
        reviews=FaultReview(),
    )
    try:
        thread, turn, call = agent_state(root)
        with pytest.raises(KernelError) as caught:
            await gateway.prepare(thread, turn, call, CancelToken())
        assert type(caught.value) is KernelError
        assert caught.value.code == "trusted_action_review_failed"
        assert "internal-canary" not in caught.value.message
        plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
        assert router.status(plan_id).state == "pending_approval"
        assert router.approval(plan_id) is None
        assert executor.calls == 0
    finally:
        gateway.close()
        plans.close()
        audit.close()


@pytest.mark.parametrize(
    "race", ["approved-during-review", "approval-cas", "route-cas", "not-closed"]
)
async def test_rejection_requires_successful_route_close(tmp_path, monkeypatch, race):
    async with product(tmp_path) as (root, runtime, provider, router, _, _, _):
        other_plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db")
        other_audit = SQLiteActionAuditStore(tmp_path / "state/audit.db")
        other = TrustedActionRouter(
            plans=other_plans, audit=other_audit, workspace_root=lambda _: root
        )
        approved = ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="competing-reviewer")
        entered, release = asyncio.Event(), asyncio.Event()
        original_review = WorkspacePatchReviewProvider.review
        original_record = router._plans.record_approval
        original_transition = router._audit.transition

        async def held_review(self, *args):
            try:
                return await original_review(self, *args)
            except WorkspacePatchPreconditionError:
                entered.set()
                await release.wait()
                raise

        def competing_record(checkpoint):
            other.decide(checkpoint.plan_id, approved)
            original_record(checkpoint)

        def failed_transition(*args, **kwargs):
            if kwargs["target"] == "denied":
                raise KernelError("action_route_conflict", "CAS未提交")
            return original_transition(*args, **kwargs)

        if race == "approved-during-review":
            monkeypatch.setattr(WorkspacePatchReviewProvider, "review", held_review)
        elif race == "approval-cas":
            monkeypatch.setattr(router._plans, "record_approval", competing_record)
        elif race == "route-cas":
            monkeypatch.setattr(router._audit, "transition", failed_transition)
        else:
            monkeypatch.setattr(router, "decide", lambda plan_id, *_args: router.status(plan_id))
        task = asyncio.create_task(patch_turn(runtime, provider, root, bad_proposal()))
        try:
            async with asyncio.timeout(5):
                if race == "approved-during-review":
                    await entered.wait()
                    route = router._audit.active()[0]
                    other.decide(route.plan.execution.plan_id, approved)
                    release.set()
                thread, turn, call = await task
            plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
            assert turn.status is TurnStatus.INTERRUPTED and result(turn).outcome == "unknown"
            assert result(turn).error.code != CODE
            checkpoint = router.approval(plan_id)
            if race in {"approved-during-review", "approval-cas"}:
                assert router.status(plan_id).state == "ready"
                assert checkpoint.decision.outcome is ApprovalOutcome.APPROVED
                assert checkpoint.decision.actor == "competing-reviewer"
            else:
                assert router.status(plan_id).state == "pending_approval"
                assert (checkpoint is None) == (race == "not-closed")
            assert all(event.executor_id is None for event in router.events(plan_id))
            assert_unchanged(root)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            other_plans.close()
            other_audit.close()


@pytest.mark.parametrize("kind", ["same-code", "typed"])
async def test_executor_error_is_still_unknown_after_approval(tmp_path, monkeypatch, kind):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        thread, waiting, _ = await patch_turn(runtime, provider, root, _proposal())
        request = _approval(waiting)
        assert waiting.status is TurnStatus.WAITING_APPROVAL
        calls = 0

        async def fail_execute(*_args):
            nonlocal calls
            calls += 1
            raise (
                WorkspacePatchPreconditionError()
                if kind == "typed"
                else KernelError(CODE, "Workspace Patch前置条件不成立")
            )

        monkeypatch.setattr(WorkspacePatchActionExecutor, "execute", fail_execute)
        await runtime.reply_approval(
            thread.thread_id,
            waiting.turn_id,
            request.approval_id,
            fingerprint=request.request_fingerprint,
            decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="user"),
        )
        completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
        assert completed.status is TurnStatus.INTERRUPTED and result(completed).outcome == "unknown"
        assert router.status(request.plan_id).state == "unknown"
        assert router.events(request.plan_id)[-1].error_code == "unexpected_write_error"
        assert transactions.load(request.plan_id).state == "prepared"
        assert calls == 1
        assert_unchanged(root)


@pytest.mark.parametrize(
    "state", ["unmarked-pending", "approved-pending", "ready", "running", "unknown"]
)
async def test_stale_review_cannot_downgrade_approved_or_started_route(
    tmp_path, monkeypatch, state
):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, composition, _):
        _, waiting, call = await patch_turn(runtime, provider, root, _proposal())
        route = router.status(_approval(waiting).plan_id)
        plan_id = route.plan.execution.plan_id
        approved = ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="user")
        if state == "approved-pending":
            with monkeypatch.context() as patch:

                def fail_transition(*_args, **_kwargs):
                    raise KernelError("action_route_conflict", "未提交Route")

                patch.setattr(router._audit, "transition", fail_transition)
                with pytest.raises(KernelError):
                    router.decide(plan_id, approved)
        elif state != "unmarked-pending":
            router.decide(plan_id, approved)
            if state in {"running", "unknown"}:
                router._audit.transition(plan_id, expected={"ready"}, target="running")
            if state == "unknown":
                router._audit.transition(plan_id, expected={"running"}, target="unknown")
        before, events, approval = (
            router.status(plan_id),
            router.events(plan_id),
            router.approval(plan_id),
        )
        error = WorkspacePatchPreconditionError()
        if state != "unmarked-pending":
            record = transactions.load(plan_id)
            mutation = next(
                item for item in record.plan.mutations if item.path == "src/modified.py"
            )
            with pytest.raises(WorkspacePatchPreconditionError) as mismatch:
                _validate_mutation(
                    _proposal().files[0].model_copy(update={"expected_sha256": "0" * 64}), mutation
                )
            error = mismatch.value
            assert is_workspace_patch_sha_mismatch(error)
        error.review_plan_id = plan_id
        assert (
            workspace_patch_preparation_rejection(
                router,
                route.plan.binding,
                call,
                error,
                route,
                composition.gateway._state.reviews["apply_patch_batch"],
            )
            is None
        )
        assert router.status(plan_id) == before and router.events(plan_id) == events
        assert router.approval(plan_id) == approval


async def test_only_original_before_sha_check_uses_dedicated_type(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, _, transactions, _, _):
        _, waiting, _ = await patch_turn(runtime, provider, root, _proposal())
        record = transactions.load(_approval(waiting).plan_id)
        mutation = next(item for item in record.plan.mutations if item.path == "src/modified.py")
        item = _proposal().files[0]
        with pytest.raises(WorkspacePatchPreconditionError) as mismatch:
            _validate_mutation(item.model_copy(update={"expected_sha256": "0" * 64}), mutation)
        assert mismatch.value.review_plan_id is None
        assert is_workspace_patch_sha_mismatch(mismatch.value)
        for invalid in (
            mutation.model_copy(update={"before": WorkspaceFileVersion(presence="absent", size=0)}),
            mutation.model_copy(
                update={"after": mutation.after.model_copy(update={"sha256": "0" * 64})}
            ),
        ):
            with pytest.raises(KernelError) as generic:
                _validate_mutation(item, invalid)
            assert type(generic.value) is KernelError and generic.value.code == CODE
            assert generic.value.message == "Workspace Patch前置条件不成立"


async def test_denied_route_recovers_before_failed_result_is_persisted(tmp_path, monkeypatch):
    class HostInterrupted(BaseException):
        pass

    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        executor_calls = 0
        interrupted = False

        async def forbidden_execute(*_args):
            nonlocal executor_calls
            executor_calls += 1
            pytest.fail("拒绝恢复不得启动Executor")

        def stop_before_result(point):
            nonlocal interrupted
            if point == "runtime.after_trusted_action_prepare" and not interrupted:
                interrupted = True
                raise HostInterrupted()

        monkeypatch.setattr(WorkspacePatchActionExecutor, "execute", forbidden_execute)
        monkeypatch.setattr(
            runtime._trusted_actions,
            "_state",
            replace(runtime._trusted_actions._state, fault=stop_before_result),
        )
        provider.steps = (_action_step(bad_proposal()),)
        thread = await runtime.create_thread(str(root))
        with pytest.raises(HostInterrupted):
            await runtime.run_turn(thread.thread_id, "修改文件", request_id="denied-gap")
        gap = await runtime.store.get_thread(thread.thread_id)
        turn = gap.turns[-1]
        call = next(
            item.content for item in turn.items if isinstance(item.content, ToolCallContent)
        )
        plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
        assert router.status(plan_id).state == "denied"
        assert not any(isinstance(item.content, ToolResultContent) for item in turn.items)
        events, approval = router.events(plan_id), router.approval(plan_id)

        def forbidden_decide(*_args, **_kwargs):
            pytest.fail("拒绝恢复不得重新审批")

        monkeypatch.setattr(router, "decide", forbidden_decide)
        await runtime._recover(gap)
        recovered = (await runtime.store.get_thread(thread.thread_id)).turns[-1]
        assert result(recovered).outcome == "failed"
        assert not any(
            isinstance(item.content, TrustedActionApprovalRequestContent)
            for item in recovered.items
        )
        assert router.status(plan_id).state == "denied"
        assert router.events(plan_id) == events and router.approval(plan_id) == approval
        assert executor_calls == 0 and len(provider.requests) == 1
        with pytest.raises(KernelError) as missing:
            transactions.load(plan_id)
        assert missing.value.code == "delivery_transaction_not_found"
        assert_unchanged(root)


@pytest.mark.parametrize("cancellation", ["token", "task"])
async def test_cancelled_review_never_becomes_sha_failed(tmp_path, monkeypatch, cancellation):
    async with product(tmp_path) as (root, _, _, router, _, composition, _):
        thread, turn, original_call = agent_state(root)
        tool = next(
            item for item in composition.gateway.definitions() if item.name == "apply_patch_batch"
        )
        call = original_call.model_copy(
            update={
                "tool": tool.name,
                "tool_version": tool.version,
                "tool_fingerprint": next(
                    item.tool_fingerprint for item in router.bindings() if item.tool == tool.name
                ),
                "arguments": bad_proposal().model_dump(mode="json"),
            }
        )
        entered, closed = asyncio.Event(), asyncio.Event()

        async def wait_review(*_args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        monkeypatch.setattr(WorkspacePatchReviewProvider, "review", wait_review)
        token = CancelToken()
        task = asyncio.create_task(composition.gateway.prepare(thread, turn, call, token))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
                token.cancel() if cancellation == "token" else task.cancel()
                with pytest.raises(
                    TurnCancelled if cancellation == "token" else asyncio.CancelledError
                ):
                    await task
                await closed.wait()
            plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
            assert router.status(plan_id).state == "pending_approval"
            assert router.approval(plan_id) is None
            assert_unchanged(root)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
