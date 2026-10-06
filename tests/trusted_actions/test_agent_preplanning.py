"""可信异步预规划：真实SQLite持久化、完整观察、取消与查询优先回归。"""

from __future__ import annotations

import asyncio
import inspect
import traceback
from dataclasses import fields, replace
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TrustedActionApprovalRequestContent
from harnessix.domain.models import EffectClass, PolicyDecisionKind
from harnessix.trusted_actions.agent_preplanning import plan_agent_action
from harnessix.trusted_actions.planning import plan_action
from harnessix.trusted_actions.router import ResolvedAction, TrustedActionDefinition
from harnessix.workspace import snapshot_v2
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from tests.support.agent_preplanning import (
    PreparedFileInput,
    RecordingPreparer,
    UnrenderablePreparationError,
    file_resolution,
    preplanning_harness,
)

_PRIVATE_CANARY = "private-preparation-canary-/sensitive/tenant/repository"


def test_new_fields_preserve_old_positional_contracts_and_async_entrypoint() -> None:
    assert [item.name for item in fields(TrustedActionDefinition)][-1] == "agent_prepare"
    assert fields(TrustedActionDefinition)[-1].default is None
    assert [item.name for item in fields(ResolvedAction)][-1] == "expected_workspace"
    assert fields(ResolvedAction)[-1].default is None
    assert inspect.iscoroutinefunction(plan_agent_action)
    assert tuple(inspect.signature(plan_agent_action).parameters) == (
        "router",
        "invocation",
        "context",
        "thread",
        "turn",
        "call",
        "cancel",
    )


async def test_preparation_then_policy_actual_snapshot_route_and_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = RecordingPreparer()
    checkpoints: list[str] = []
    with preplanning_harness(
        tmp_path, provider, checkpoint=lambda: checkpoints.append("check")
    ) as h:
        actual_capture = snapshot_v2.capture_snapshot_facts

        def observe(*args: object, **kwargs: object):
            h.trace.append("snapshot")
            return actual_capture(*args, **kwargs)

        monkeypatch.setattr(snapshot_v2, "capture_snapshot_facts", observe)
        token = CancelToken()
        call = h.call.model_copy(update={"arguments": {}})
        approval = await h.gateway.prepare(h.thread, h.turn, call, token)

        assert isinstance(approval, TrustedActionApprovalRequestContent)
        assert h.trace == ["prepare", "policy", "snapshot", "route", "execution", "review"]
        route = h.router.status(approval.plan_id)
        invocation, arguments, context, thread, turn, observed_call, cancel = provider.observed[0]
        assert invocation == h.invocation(call).model_copy(
            update={"arguments": {"path": "file.txt"}}
        )
        assert type(arguments) is PreparedFileInput and arguments.path == "file.txt"
        assert thread == h.thread and turn == h.turn and observed_call == call and cancel is token
        assert context is not h.context and context.checkpoint is not h.context.checkpoint
        assert replace(context, checkpoint=h.context.checkpoint) == h.context and checkpoints
        assert (
            route.state == "pending_approval"
            and route.plan.resources == file_resolution().resources
        )
        assert route.plan.execution.workspace.spec_version == "harnessix.workspace-snapshot/v2"
        assert route.plan.execution.policy.decision is PolicyDecisionKind.REQUIRE_APPROVAL
        assert route.plan.binding.executor_id == h.binding.executor_id
        assert h.plans.load_plan(approval.plan_id) == route.plan.execution
        assert (
            len(h.router.events(approval.plan_id)) == 1
            and h.router.approval(approval.plan_id) is None
        )
        assert provider.calls == provider.settled == h.review.calls == 1
        h.assert_no_effect()


@pytest.mark.parametrize("use_v2", [False, True])
async def test_no_preparer_keeps_original_resolve_path(tmp_path: Path, use_v2: bool) -> None:
    with preplanning_harness(tmp_path, use_v2=use_v2) as h:
        approval = await h.gateway.prepare(h.thread, h.turn, h.call, CancelToken())
        assert isinstance(approval, TrustedActionApprovalRequestContent)
        assert h.trace == ["resolve", "policy", "route", "execution", "review"]
        route = h.router.status(approval.plan_id)
        assert route.plan.invocation == h.invocation()
        assert route.plan.execution.workspace.spec_version == (
            "harnessix.workspace-snapshot/v2" if use_v2 else "harnessix.workspace-snapshot/v1"
        )


@pytest.mark.parametrize("control", ["token", "parent"])
async def test_suspended_preparation_cancellation_reaps_child_and_leaves_no_route(
    tmp_path: Path, control: str
) -> None:
    release, cleaned = asyncio.Event(), asyncio.Event()

    async def suspend(*_args: object) -> ResolvedAction:
        try:
            await release.wait()
            return file_resolution()
        finally:
            cleaned.set()

    provider = RecordingPreparer(suspend)
    with preplanning_harness(tmp_path, provider) as h:
        original_tasks = asyncio.all_tasks()
        token = CancelToken()
        task = asyncio.create_task(h.gateway.prepare(h.thread, h.turn, h.call, token))
        try:
            await asyncio.wait_for(provider.entered.wait(), timeout=5)
            token.cancel() if control == "token" else task.cancel()
            with pytest.raises(TurnCancelled if control == "token" else asyncio.CancelledError):
                await task
            assert cleaned.is_set() and provider.finished.is_set() and provider.settled == 1
            assert asyncio.all_tasks() <= original_tasks
            h.assert_unplanned()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_original_router_deadline_reaps_suspended_preparation(tmp_path: Path) -> None:
    cleaned = asyncio.Event()

    async def suspend(*_args: object) -> ResolvedAction:
        try:
            await asyncio.Event().wait()
            raise AssertionError("等待不得正常返回")
        finally:
            cleaned.set()

    provider = RecordingPreparer(suspend)
    with preplanning_harness(tmp_path, provider) as h:
        h.router._execute_timeout_seconds = 0.02
        original_tasks = asyncio.all_tasks()
        with pytest.raises(KernelError) as caught:
            async with asyncio.timeout(2):
                await h.gateway.prepare(h.thread, h.turn, h.call, CancelToken())
        assert caught.value.code == "action_plan_failed" and not caught.value.retryable
        assert cleaned.is_set() and provider.settled == 1
        assert asyncio.all_tasks() <= original_tasks
        h.assert_unplanned()


async def test_already_cancelled_token_never_enters_preparer(tmp_path: Path) -> None:
    provider, token = RecordingPreparer(), CancelToken()
    token.cancel()
    with preplanning_harness(tmp_path, provider) as h:
        with pytest.raises(TurnCancelled):
            await h.gateway.prepare(h.thread, h.turn, h.call, token)
        assert provider.calls == 0
        h.assert_unplanned()


@pytest.mark.parametrize("kind", ["timeout", "value", "kernel", "turn", "task"])
@pytest.mark.parametrize("stage", ["before_provider", "inside_provider"])
async def test_original_host_checkpoint_exception_preserves_object_identity(
    tmp_path: Path, kind: str, stage: str
) -> None:
    failures = {
        "timeout": TimeoutError(_PRIVATE_CANARY),
        "value": ValueError(_PRIVATE_CANARY),
        "kernel": KernelError("host_deadline", _PRIVATE_CANARY),
        "turn": TurnCancelled(_PRIVATE_CANARY),
        "task": asyncio.CancelledError(_PRIVATE_CANARY),
    }
    failure = failures[kind]
    provider = RecordingPreparer()

    def checkpoint() -> None:
        if stage == "before_provider" or provider.entered.is_set():
            raise failure

    with preplanning_harness(tmp_path, provider, checkpoint=checkpoint) as h:
        with pytest.raises(type(failure)) as caught:
            await h.gateway.prepare(h.thread, h.turn, h.call, CancelToken())
        assert caught.value is failure
        assert provider.calls == (0 if stage == "before_provider" else 1)
        h.assert_unplanned()


@pytest.mark.parametrize(
    "kind", ["runtime", "value", "type", "timeout", "kernel", "resolver_code", "unrenderable"]
)
async def test_provider_exception_has_fixed_public_error_and_no_private_text(
    tmp_path: Path, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    errors = {
        "runtime": RuntimeError(_PRIVATE_CANARY),
        "value": ValueError(_PRIVATE_CANARY),
        "type": TypeError(_PRIVATE_CANARY),
        "timeout": TimeoutError(_PRIVATE_CANARY),
        "kernel": KernelError("private_preparation_code", _PRIVATE_CANARY, retryable=True),
        "resolver_code": KernelError("workspace_path_denied", _PRIVATE_CANARY, retryable=True),
        "unrenderable": UnrenderablePreparationError("private_preparation_code", _PRIVATE_CANARY),
    }

    async def reject(*_args: object) -> ResolvedAction:
        raise errors[kind]

    provider = RecordingPreparer(reject)
    with preplanning_harness(tmp_path, provider) as h:
        with pytest.raises(KernelError) as caught:
            await h.gateway.prepare(h.thread, h.turn, h.call, CancelToken())
        error = caught.value
        assert error is not errors[kind] and error.code == "action_plan_failed"
        assert error.message == "Action计划阶段失败；内部原因不公开" and not error.retryable
        assert error.__suppress_context__ and error.__cause__ is None
        public = "\n".join(
            (
                str(error),
                error.to_failure().model_dump_json(),
                "".join(traceback.format_exception(error)),
                caplog.text,
            )
        )
        assert _PRIVATE_CANARY not in public
        for store in (h.plans, h.audit, h.cas):
            assert _PRIVATE_CANARY not in "\n".join(store._db.iterdump())
        h.assert_unplanned()


@pytest.mark.parametrize("kind", ["turn", "task"])
async def test_provider_cancellation_is_not_sanitized(tmp_path: Path, kind: str) -> None:
    failure = TurnCancelled() if kind == "turn" else asyncio.CancelledError()

    async def reject(*_args: object) -> ResolvedAction:
        raise failure

    with preplanning_harness(tmp_path, RecordingPreparer(reject)) as h:
        with pytest.raises(type(failure)) as caught:
            await h.gateway.prepare(h.thread, h.turn, h.call, CancelToken())
        assert caught.value is failure
        h.assert_unplanned()


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": 3},
        {"path": ""},
        {"path": "x" * 257},
        {"path": "file.txt", "unexpected": 1},
        {"api_key": "synthetic-private-key"},
        {"invocation_id": "model-controlled"},
        {"plan_id": "model-controlled"},
        {"idempotency_key": "model-controlled"},
        {"policy": "allow"},
        {"executor": "model-controlled"},
        {"workspace_root": "/unbound"},
    ],
)
async def test_invalid_or_model_control_arguments_never_enter_preparer(
    tmp_path: Path, arguments: dict
) -> None:
    provider = RecordingPreparer()
    with preplanning_harness(tmp_path, provider) as h:
        call = h.call.model_copy(update={"arguments": arguments})
        with pytest.raises(KernelError) as caught:
            await h.gateway.prepare(h.thread, h.turn, call, CancelToken())
        assert caught.value.code == (
            "raw_secret_rejected" if "api_key" in arguments else "tool_invalid_arguments"
        )
        assert provider.calls == 0
        h.assert_unplanned()


@pytest.mark.parametrize(
    "change",
    [
        {"tool": "unregistered.tool"},
        {"tool_version": "other"},
        {"tool_fingerprint": "0" * 64},
        {"effect_class": EffectClass.READ_ONLY},
        {"requires_approval": False},
    ],
)
async def test_invalid_tool_binding_never_enters_preparer(tmp_path: Path, change: dict) -> None:
    provider = RecordingPreparer()
    with preplanning_harness(tmp_path, provider) as h:
        with pytest.raises(KernelError) as caught:
            await h.gateway.prepare(
                h.thread, h.turn, h.call.model_copy(update=change), CancelToken()
            )
        assert caught.value.code in {
            "trusted_tool_contract_changed",
            "trusted_action_not_registered",
        }
        assert provider.calls == 0
        h.assert_unplanned()


@pytest.mark.parametrize(
    "replacement",
    [
        {"idempotency_key": None},
        {"idempotency_key": "unbound"},
        {"arguments": {"path": "other.txt"}},
    ],
)
async def test_direct_entrypoint_requires_original_builder_scope_before_provider(
    tmp_path: Path, replacement: dict
) -> None:
    provider = RecordingPreparer()
    with preplanning_harness(tmp_path, provider) as h:
        with pytest.raises(KernelError) as caught:
            await plan_agent_action(
                h.router,
                h.invocation().model_copy(update=replacement),
                h.context,
                h.thread,
                h.turn,
                h.call,
                CancelToken(),
            )
        assert caught.value.code == "trusted_action_plan_mismatch" and provider.calls == 0
        h.assert_unplanned()


async def test_preparer_cannot_mutate_original_call_or_stable_plan_identity(tmp_path: Path) -> None:
    async def mutate(
        invocation, arguments, _context, thread, turn, call, _cancel
    ) -> ResolvedAction:
        vars(invocation)["invocation_id"] = uuid4()
        vars(invocation)["idempotency_key"] = "untrusted-replacement"
        invocation.arguments["path"] = "changed-invocation.txt"
        vars(arguments)["path"] = "changed-by-provider.txt"
        vars(thread)["workspace"] = "/other-root"
        vars(turn)["turn_id"] = uuid4()
        call.arguments["path"] = "changed-call.txt"
        return file_resolution()

    with preplanning_harness(tmp_path, RecordingPreparer(mutate)) as h:
        selected_call = h.call.model_copy(update={"arguments": {}})
        original = h.invocation(selected_call)
        original_call = selected_call.model_dump_json()
        approval = await h.gateway.prepare(h.thread, h.turn, selected_call, CancelToken())
        assert isinstance(approval, TrustedActionApprovalRequestContent)
        assert h.router.status(approval.plan_id).plan.invocation == original.model_copy(
            update={"arguments": {"path": "file.txt"}}
        )
        assert selected_call.model_dump_json() == original_call and selected_call.arguments == {}
        assert h.call.arguments == {"path": "file.txt"} and original.arguments == {}
        assert h.thread.workspace == str(h.root) and h.thread.active_turn_id == h.turn.turn_id


@pytest.mark.parametrize("damage", ["none", "content", "inode", "parent_history", "no_v2_ports"])
async def test_expected_workspace_must_equal_complete_actual_snapshot(
    tmp_path: Path, damage: str
) -> None:
    observations = {}

    async def prepare(_invocation, _arguments, context, *_rest) -> ResolvedAction:
        result = file_resolution("nested/file.txt")
        expected = h.snapshot(result.workspace_resources)
        observations["expected"] = expected
        if damage == "content":
            target.write_text("after", encoding="utf-8")
        elif damage == "inode":
            replacement = tmp_path / "replacement.txt"
            replacement.write_bytes(target.read_bytes())
            with target.open("rb"):
                replacement.replace(target)
        elif damage == "parent_history":
            (target.parent / "new-sibling.txt").write_text("unselected", encoding="utf-8")
        current = h.snapshot(result.workspace_resources)
        observations["current"] = current
        assert context.workspace_root == h.root
        return replace(result, expected_workspace=expected)

    provider = RecordingPreparer(prepare)
    with preplanning_harness(tmp_path, provider, use_v2=damage != "no_v2_ports") as h:
        target = h.root / "nested/file.txt"
        target.parent.mkdir()
        target.write_text("before", encoding="utf-8")
        call = h.call.model_copy(update={"arguments": {"path": "nested/file.txt"}})
        if damage == "none":
            approval = await h.gateway.prepare(h.thread, h.turn, call, CancelToken())
            assert isinstance(approval, TrustedActionApprovalRequestContent)
            assert (
                h.router.status(approval.plan_id).plan.execution.workspace
                == observations["expected"]
            )
        else:
            with pytest.raises(KernelError) as caught:
                await h.gateway.prepare(h.thread, h.turn, call, CancelToken())
            assert caught.value.code == "action_preparation_workspace_changed"
            h.assert_unplanned()
        expected, current = observations["expected"], observations["current"]
        before = next(item for item in expected.resources if item.path == "nested/file.txt")
        after = next(item for item in current.resources if item.path == "nested/file.txt")
        if damage == "content":
            assert before.content_sha256 != after.content_sha256
        elif damage == "inode":
            assert (
                before.content_sha256 == after.content_sha256 and before.identity != after.identity
            )
        elif damage == "parent_history":
            assert (
                expected.resources == current.resources
                and expected.parent_closure != current.parent_closure
            )
            assert read_workspace_parent_closure(
                expected, h.cas.blob, checkpoint=lambda: None
            ) != read_workspace_parent_closure(current, h.cas.blob, checkpoint=lambda: None)


async def test_restarted_same_identity_queries_route_before_preparer_and_repairs_execution(
    tmp_path: Path,
) -> None:
    provider = RecordingPreparer()
    with preplanning_harness(tmp_path, provider) as h:
        approval = await h.gateway.prepare(h.thread, h.turn, h.call, CancelToken())
        assert isinstance(approval, TrustedActionApprovalRequestContent)
        saved = h.router.status(approval.plan_id)
        state = (h.thread, h.turn, h.call)
        h.plans._db.execute("DELETE FROM execution_plans WHERE plan_id=?", (str(approval.plan_id),))
        assert len(h.router.events(approval.plan_id)) == 1

    async def forbidden(*_args: object) -> ResolvedAction:
        raise AssertionError("查询原Route必须先于重新观察")

    never = RecordingPreparer(forbidden)
    with preplanning_harness(tmp_path, never, state=state) as restored:
        restored.root.joinpath("file.txt").write_text(
            "changed after persisted route", encoding="utf-8"
        )
        replayed = await restored.gateway.prepare(*state, CancelToken())
        assert replayed == approval and never.calls == 0 and "resolve" not in restored.trace
        assert "policy" not in restored.trace and "route" not in restored.trace
        assert restored.plans.load_plan(approval.plan_id) == saved.plan.execution
        assert restored.router.status(approval.plan_id) == saved
        assert len(restored.router.events(approval.plan_id)) == 1
        assert restored.router.plan(restored.invocation(), restored.context) == saved


async def test_same_identity_different_arguments_conflict_before_preparer(tmp_path: Path) -> None:
    provider = RecordingPreparer()
    with preplanning_harness(tmp_path, provider) as h:
        first = await h.gateway.prepare(h.thread, h.turn, h.call, CancelToken())
        assert isinstance(first, TrustedActionApprovalRequestContent)
        conflict = h.call.model_copy(update={"arguments": {"path": "other.txt"}})
        with pytest.raises(KernelError) as caught:
            await h.gateway.prepare(h.thread, h.turn, conflict, CancelToken())
        assert caught.value.code == "action_invocation_conflict"
        assert provider.calls == h.review.calls == 1 and len(h.router.events(first.plan_id)) == 1


def test_sync_plan_cannot_bypass_registered_agent_preparer(tmp_path: Path) -> None:
    provider = RecordingPreparer()
    with preplanning_harness(tmp_path, provider) as h:
        with pytest.raises(KernelError) as caught:
            h.router.plan(h.invocation(), h.context)
        assert caught.value.code == "action_preparation_required" and provider.calls == 0
        assert h.trace == []
        h.assert_unplanned()


async def test_prepared_resources_cannot_override_original_policy(tmp_path: Path) -> None:
    async def prepare(*_args: object) -> ResolvedAction:
        return ResolvedAction(resources=())

    with preplanning_harness(tmp_path, RecordingPreparer(prepare)) as h:
        await h.gateway.prepare(h.thread, h.turn, h.call, CancelToken())
        route = h.router.status(h.invocation().invocation_id)
        assert route.state == "denied"
        assert route.plan.execution.policy.decision is PolicyDecisionKind.DENY
        assert route.plan.execution.policy.reason_code == "write_resource_missing"
        assert route.plan.binding.executor_id == h.binding.executor_id and h.review.calls == 0
        h.assert_no_effect()


@pytest.mark.parametrize(
    "shape",
    [
        "none",
        "dict",
        "duck_with_context",
        "subclass",
        "resources_list",
        "resource_dict",
        "resource_subclass",
        "resource_incomplete",
        "resource_extra",
        "resource_invalid",
        "resource_wrong_primitive",
        "workspace_list",
        "workspace_dict",
        "workspace_subclass",
        "workspace_extra",
        "workspace_invalid",
        "workspace_invalid_access",
        "legacy_expected",
        "expected_subclass",
        "expected_extra",
        "expected_incomplete",
        "expected_invalid_revision",
        "expected_nested_extra",
        "expected_nested_wrong_primitive",
    ],
)
async def test_bad_preparer_return_is_fixed_invalid_before_policy_or_persistence(
    tmp_path: Path, shape: str
) -> None:
    from tests.support.agent_preplanning import invalid_resolution

    async def prepare(*_args: object) -> ResolvedAction:
        return invalid_resolution(h, shape)

    with preplanning_harness(tmp_path, RecordingPreparer(prepare)) as h:
        with pytest.raises(KernelError) as caught:
            await h.gateway.prepare(h.thread, h.turn, h.call, CancelToken())
        assert caught.value.code == "action_preparation_invalid"
        assert caught.value.message == "Action可信准备结果不符合契约"
        assert _PRIVATE_CANARY not in str(caught.value) and not caught.value.retryable
        assert "policy" not in h.trace
        h.assert_unplanned()


@pytest.mark.parametrize("difference", ["canonical", "workspace"])
async def test_route_won_during_preparation_cannot_reuse_different_resources(
    tmp_path: Path, difference: str
) -> None:
    release = asyncio.Event()

    async def delayed(*_args: object) -> ResolvedAction:
        await release.wait()
        result = file_resolution("file.txt")
        if difference == "workspace":
            result = replace(
                result,
                workspace_resources=(WorkspaceResourceRequest(path="other.txt", access="write"),),
            )
        return result

    provider = RecordingPreparer(delayed)
    with preplanning_harness(tmp_path, provider) as h:
        task = asyncio.create_task(h.gateway.prepare(h.thread, h.turn, h.call, CancelToken()))
        try:
            await asyncio.wait_for(provider.entered.wait(), timeout=5)
            definition = h.router._definition(h.binding.source, h.binding.source_id, h.binding.tool)
            winning = file_resolution("other.txt" if difference == "canonical" else "file.txt")
            winner = plan_action(
                h.invocation(),
                h.context,
                replace(definition, agent_prepare=None),
                policy=h.router._policy,
                plans=h.plans,
                audit=h.audit,
                prepared=winning,
            )
            release.set()
            with pytest.raises(KernelError) as caught:
                await task
            assert caught.value.code in {"action_invocation_conflict", "action_route_plan_conflict"}
            assert h.router.status(winner.plan.execution.plan_id) == winner
            assert h.plans.load_plan(winner.plan.execution.plan_id) == winner.plan.execution
            assert h.review.calls == 0 and len(h.router.events(winner.plan.execution.plan_id)) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("drifting", [False, True])
async def test_decoder_runs_once_and_route_uses_exact_prepared_arguments(tmp_path: Path, drifting):
    """显式Decoder只能执行一次；Route不能在准备后悄悄规范化为另一输入。"""
    calls = 0

    def decode(arguments):
        nonlocal calls
        calls += 1
        return PreparedFileInput(path="other.txt" if drifting and calls > 1 else arguments["path"])

    provider = RecordingPreparer()
    with preplanning_harness(tmp_path, provider, decode_arguments=decode) as h:
        request = await h.gateway.prepare(h.thread, h.turn, h.call, CancelToken())
        assert isinstance(request, TrustedActionApprovalRequestContent)
        route = h.router.status(request.plan_id)
        assert calls == 1
        assert (
            route.plan.invocation.arguments == provider.observed[0][0].arguments == h.call.arguments
        )
        assert provider.observed[0][1].path == "file.txt"
        assert provider.calls == 1 and h.review.calls == 1
        h.assert_no_effect()
