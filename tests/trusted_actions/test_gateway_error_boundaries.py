"""Gateway回调异常边界：公开错误、取消传播和双账本事实不能混为一体。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import NoReturn

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TrustedActionApprovalRequestContent
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, EffectClass, RiskLevel
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, ActionRouteSnapshot
from harnessix.trusted_actions.public_errors import _GATEWAY_ERRORS, sanitize_gateway_exception
from tests.trusted_actions.test_agent_gateway import (
    FakeExecutor,
    agent_state,
    build_gateway,
    descriptor,
    runtime_context,
)
from tests.trusted_actions.test_public_error_leakage import _assert_no_leak, _payload, _store_bytes


class FaultCallbacks:
    """合成回调故障，不修改Workspace，不记录或发送真实凭据。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0
        self.route: ActionRouteSnapshot | None = None

    def context(self, *_args: object) -> NoReturn:
        self.calls += 1
        raise self.error

    async def review(
        self, route: ActionRouteSnapshot, *_args: object, **_kwargs: object
    ) -> NoReturn:
        self.route = route
        self.calls += 1
        raise self.error

    async def output(
        self, route: ActionRouteSnapshot, *_args: object, **_kwargs: object
    ) -> NoReturn:
        self.route = route
        self.calls += 1
        raise self.error


def fault_gateway(
    root: Path,
    stage: str,
    error: BaseException,
    *,
    read_only: bool = False,
    executor: FakeExecutor | None = None,
):
    executor = executor or FakeExecutor(
        ActionExecutionOutcome(
            kind="succeeded", output={"summary": "completed"}, artifact_sha256="b" * 64
        )
    )
    tool = descriptor()
    if read_only:
        tool = tool.model_copy(
            update={
                "effect_class": EffectClass.READ_ONLY,
                "risk_level": RiskLevel.LOW,
                "requires_idempotency": False,
                "requires_approval": False,
                "supports_reconciliation": False,
            }
        )
    original, actions, plans, audit = build_gateway(root, executor, tool=tool)
    original.close()
    callbacks = FaultCallbacks(error)
    gateway = RouterBackedAgentActionGateway(
        actions,
        (tool,),
        callbacks.context if stage == "context" else lambda *_: runtime_context(root),
        presentations={"workspace.patch": "process"} if stage == "output" else None,
        reviews=callbacks if stage == "review" else None,
        outputs={"workspace.patch": callbacks} if stage == "output" else None,
    )
    return gateway, actions, plans, audit, executor, callbacks


async def invoke_callback(
    gateway, actions, root: Path, stage: str, *, recovery: bool, token: CancelToken | None = None
):
    token = token or CancelToken()
    thread, turn, call = agent_state(root)
    prepared = await gateway.prepare(thread, turn, call, token)
    assert isinstance(prepared, TrustedActionApprovalRequestContent)
    approved = gateway.decide(
        thread,
        turn,
        call,
        prepared,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="boundary-test"),
    )
    assert stage == "output"
    if recovery:
        await actions.execute(prepared.plan_id)
        return await gateway.recover(thread, turn, call, approved, token)
    return await gateway.execute(thread, turn, call, approved, token)


CASES = [
    pytest.param("context", False, id="context"),
    pytest.param("review", False, id="review"),
    pytest.param("output", False, id="output-execution"),
    pytest.param("output", True, id="output-recovery"),
]


@pytest.mark.parametrize("stage,recovery", CASES)
@pytest.mark.parametrize("error_kind", ["unknown", "cross_stage", "runtime", "timeout"])
async def test_gateway_callback_errors_are_fixed_and_keep_durable_facts(
    tmp_path: Path, stage: str, recovery: bool, error_kind: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "file.txt"
    source.write_text("unchanged", encoding="utf-8")
    error = (
        RuntimeError(_payload())
        if error_kind == "runtime"
        else TimeoutError(_payload())
        if error_kind == "timeout"
        else KernelError(
            "secret_in_code_canary" if error_kind == "unknown" else "workspace_path_denied",
            _payload(),
            retryable=True,
        )
    )
    gateway, actions, plans, audit, executor, callbacks = fault_gateway(root, stage, error)
    try:
        with pytest.raises(KernelError) as caught:
            await invoke_callback(gateway, actions, root, stage, recovery=recovery)
        public = caught.value
        assert public is not error
        assert public.code == f"trusted_action_{stage}_failed"
        assert not public.retryable and public.__suppress_context__
        assert callbacks.calls == 1
        routes = audit.active()
        if stage == "context":
            assert not routes and executor.calls == 0
        elif stage == "review":
            assert len(routes) == 1 and routes[0].state == "pending_approval"
            assert executor.calls == 0
            assert actions.approval(routes[0].plan.execution.plan_id) is None
        else:
            # 投影失败不否定已成功执行的事实，也不能触发重新执行或对账。
            assert executor.calls == 1 and executor.reconciliations == 0
            assert callbacks.route is not None
            plan_id = callbacks.route.plan.execution.plan_id
            assert actions.status(plan_id).state == "succeeded"
            event = actions.events(plan_id)[-1]
            assert event.error_code is None and event.artifact_sha256 == "b" * 64
        assert source.read_text(encoding="utf-8") == "unchanged"
        _assert_no_leak(str(public), repr(public), *_store_bytes(*root.parent.rglob("*.db*")))
    finally:
        gateway.close()
        plans.close()
        audit.close()


@pytest.mark.parametrize("stage", ["context", "review", "output"])
def test_every_gateway_registered_code_uses_only_its_fixed_message(stage) -> None:
    for code, message in _GATEWAY_ERRORS[stage].items():
        original = KernelError(code, _payload(), retryable=True)
        public = sanitize_gateway_exception(original, stage=stage)
        assert public is not original and public.code == code and public.message == message
        assert not public.retryable
        _assert_no_leak(str(public), repr(public))
    # context无例外，Review与Output不能互借对方的专属分类。
    foreign = "process_output_corrupt" if stage != "output" else "action_review_limit"
    assert sanitize_gateway_exception(KernelError(foreign, _payload()), stage=stage).code == (
        f"trusted_action_{stage}_failed"
    )


class WaitingCallbacks(FaultCallbacks):
    """使用显式进入/退出握手验证异步子任务回收，不依赖固定sleep。"""

    def __init__(self) -> None:
        super().__init__(RuntimeError("unused"))
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()

    async def output(
        self, route: ActionRouteSnapshot, *_args: object, **_kwargs: object
    ) -> NoReturn:
        self.route = route
        self.calls += 1
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.closed.set()
        raise AssertionError("不可达的等待完成")

    review = output


@pytest.mark.parametrize("stage,recovery", CASES[1:])
@pytest.mark.parametrize("cancellation", ["task", "token"])
async def test_cancel_inflight_callback_drains_child_without_reexecution(
    tmp_path: Path, stage: str, recovery: bool, cancellation: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged", encoding="utf-8")
    gateway, actions, plans, audit, executor, _ = fault_gateway(root, stage, RuntimeError())
    gateway.close()
    callbacks = WaitingCallbacks()
    gateway = RouterBackedAgentActionGateway(
        actions,
        (descriptor(),),
        lambda *_: runtime_context(root),
        presentations={"workspace.patch": "process"} if stage == "output" else None,
        reviews=callbacks if stage == "review" else None,
        outputs={"workspace.patch": callbacks} if stage == "output" else None,
    )
    token = CancelToken()
    task = asyncio.create_task(
        invoke_callback(gateway, actions, root, stage, recovery=recovery, token=token)
    )
    try:
        async with asyncio.timeout(3):
            await callbacks.entered.wait()
            task.cancel() if cancellation == "task" else token.cancel()
            with pytest.raises(asyncio.CancelledError if cancellation == "task" else TurnCancelled):
                await task
            await callbacks.closed.wait()
        assert callbacks.calls == 1 and executor.reconciliations == 0
        assert callbacks.route is not None
        plan_id = callbacks.route.plan.execution.plan_id
        assert actions.status(plan_id).state == (
            "succeeded" if stage == "output" else "pending_approval"
        )
        assert executor.calls == (1 if stage == "output" else 0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        gateway.close()
        plans.close()
        audit.close()


@pytest.mark.parametrize("stage,recovery", CASES)
@pytest.mark.parametrize("cancellation", ["task", "domain"])
async def test_gateway_callback_cancellation_is_not_a_public_failure(
    tmp_path: Path, stage: str, recovery: bool, cancellation: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged", encoding="utf-8")
    error = asyncio.CancelledError() if cancellation == "task" else TurnCancelled()
    gateway, actions, plans, audit, executor, callbacks = fault_gateway(root, stage, error)
    try:
        with pytest.raises(type(error)) as caught:
            await invoke_callback(gateway, actions, root, stage, recovery=recovery)
        assert caught.value is error and callbacks.calls == 1
        assert executor.calls == (1 if stage == "output" else 0)
        assert executor.reconciliations == 0
    finally:
        gateway.close()
        plans.close()
        audit.close()
