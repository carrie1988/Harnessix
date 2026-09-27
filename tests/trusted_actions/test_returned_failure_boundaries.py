"""正常返回的失败合同必须在记账前收敛，不能借合法JSON绕过异常边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import TrustedActionApprovalRequestContent
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.trusted_actions.test_agent_gateway import FakeExecutor, agent_state
from tests.trusted_actions.test_gateway_error_boundaries import fault_gateway
from tests.trusted_actions.test_gateway_error_runtime import assert_stores_safe
from tests.trusted_actions.test_public_error_leakage import _assert_no_leak, _payload


@pytest.mark.parametrize(
    "stage,kind",
    [
        ("execute", "failed"),
        ("execute", "unknown"),
        ("reconcile", "failed"),
        ("reconcile", "unknown"),
        ("reconcile", "manual_intervention"),
    ],
)
@pytest.mark.parametrize("code", ["secret_in_code_canary", "process_timeout"])
async def test_returned_failure_is_normalized_before_audit_and_projection(
    tmp_path: Path, stage: str, kind: str, code: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "file.txt"
    source.write_text("unchanged", encoding="utf-8")
    hostile = ActionExecutionOutcome(
        kind=kind, error_code=code, output={"diagnostic": _payload()}, artifact_sha256="b" * 64
    )
    executor = FakeExecutor(
        hostile
        if stage == "execute"
        else ActionExecutionOutcome(kind="unknown", error_code="lost"),
        hostile if stage == "reconcile" else None,
    )
    gateway, actions, plans, audit, _, callbacks = fault_gateway(
        root, "execute", RuntimeError("unused"), executor=executor
    )
    try:
        thread, turn, call = agent_state(root)
        request = await gateway.prepare(thread, turn, call, CancelToken())
        assert isinstance(request, TrustedActionApprovalRequestContent)
        approved = gateway.decide(
            thread,
            turn,
            call,
            request,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="boundary-test"),
        )
        if stage == "reconcile":
            await actions.execute(request.plan_id)
            result = await gateway.recover(thread, turn, call, approved, CancelToken())
        else:
            result = await gateway.execute(thread, turn, call, approved, CancelToken())
        expected = {
            "failed": "action_failed",
            "unknown": "action_effect_unknown",
            "manual_intervention": "action_manual_intervention",
        }[kind]
        assert result.error is not None and result.error.code == expected
        assert result.output is None and result.trusted_action is not None
        assert result.trusted_action.state == kind
        assert result.trusted_action.artifact_sha256 is None
        route = actions.status(request.plan_id)
        event = actions.events(request.plan_id)[-1]
        assert route.state == kind and event.error_code == expected
        assert event.output_sha256 is None and event.artifact_sha256 is None
        assert executor.calls == 1 and executor.reconciliations == (stage == "reconcile")
        assert callbacks.calls == 0
        assert source.read_text(encoding="utf-8") == "unchanged"
        _assert_no_leak(
            result.model_dump_json(),
            *(item.model_dump_json() for item in actions.events(request.plan_id)),
        )
        assert_stores_safe(tmp_path)
    finally:
        gateway.close()
        plans.close()
        audit.close()


@pytest.mark.parametrize("code", ["secret_in_code_canary", "process_timeout"])
async def test_legacy_audit_projection_tightens_public_content_without_rewriting_hash_chain(
    tmp_path, code
):
    root = tmp_path / "workspace"
    root.mkdir()
    gateway, actions, plans, audit, executor, callbacks = fault_gateway(
        root, "output", RuntimeError(_payload())
    )
    try:
        thread, turn, call = agent_state(root)
        request = await gateway.prepare(thread, turn, call, CancelToken())
        approved = gateway.decide(
            thread,
            turn,
            call,
            request,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="legacy-test"),
        )
        # 用合法Audit API模拟旧版本写入，保持摘要链完整；不能绕过行校验伪造损坏数据。
        claim = audit.claim_operation(request.plan_id, phase="execute", timeout_seconds=30)
        audit.complete_operation(
            claim,
            target="failed",
            executor_id=actions.status(request.plan_id).plan.binding.executor_id,
            output_sha256="a" * 64,
            artifact_sha256="b" * 64,
            error_code=code,
        )
        before = actions.events(request.plan_id)
        result = await gateway.recover(thread, turn, call, approved, CancelToken())
        assert result.error.code == "action_failed" and result.output is None
        assert result.trusted_action.artifact_sha256 is None
        assert actions.events(request.plan_id) == before and before[-1].error_code == code
        assert executor.calls == 0 and executor.reconciliations == 0 and callbacks.calls == 0
        _assert_no_leak(result.model_dump_json())
        # 历史私有Audit码仍存在，不能把公开整改描述为历史存储清洗。
    finally:
        gateway.close()
        plans.close()
        audit.close()
