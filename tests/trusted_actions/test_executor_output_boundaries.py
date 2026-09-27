"""执行器原始返回值必须在编码前受预算约束，未验证声明不能直接成为审计事实。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, EffectClass, RiskLevel
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.trusted_actions.test_router import (
    FakeExecutor,
    binding,
    context,
    definition,
    invocation,
    router,
)


class SerializerOutcome(ActionExecutionOutcome):
    """模拟扩展通过DTO子类接管序列化，不允许内核调用此回调。"""

    def model_dump_json(self, **kwargs):
        raise RuntimeError("不应调用扩展序列化器")


def raw_outcome(case):
    if case == "subclass":
        return SerializerOutcome(kind="succeeded")
    if case == "header":
        return ActionExecutionOutcome.model_construct(kind="succeeded", error_code="invalid")
    value = "x" * (1024 * 1024 + 1)
    if case == "nodes":
        value = [None] * 20000
    elif case == "depth":
        value = None
        for _ in range(65):
            value = [value]
    elif case == "integer":
        value = 1 << 200
    elif case == "native":
        value = b"not-native-json"
    elif case == "cycle":
        value = []
        value.append(value)
    return ActionExecutionOutcome.model_construct(kind="succeeded", output=value)


@pytest.mark.parametrize(
    "stage,read_only", [("execute", True), ("execute", False), ("reconcile", False)]
)
@pytest.mark.parametrize(
    "case", ["bytes", "nodes", "depth", "integer", "native", "cycle", "subclass", "header"]
)
async def test_unbounded_or_invalid_executor_return_is_rejected_before_audit(
    tmp_path: Path, stage, read_only, case
):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged", encoding="utf-8")
    tool = binding(
        effect=EffectClass.READ_ONLY if read_only else EffectClass.NON_IDEMPOTENT_WRITE,
        risk=RiskLevel.LOW if read_only else RiskLevel.HIGH,
        recovery="none" if read_only else "durable_ledger",
    )
    hostile = raw_outcome(case)
    executor = FakeExecutor(
        hostile
        if stage == "execute"
        else ActionExecutionOutcome(kind="unknown", error_code="executor_error"),
        hostile if stage == "reconcile" else None,
    )
    actions, plans, audit = router(root, definition(tool, executor))
    try:
        route = actions.plan(invocation(tool), context(root))
        if not read_only:
            actions.decide(
                route.plan.execution.plan_id,
                ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="raw-budget-test"),
            )
        result = await actions.execute(route.plan.execution.plan_id)
        if stage == "reconcile":
            result = await actions.reconcile(route.plan.execution.plan_id)
        reason = "limit" if case in {"bytes", "nodes", "depth", "integer"} else "invalid"
        expected = (
            f"executor_output_{reason}"
            if read_only
            else f"write_output_{reason}_unknown"
            if stage == "execute"
            else f"reconciliation_output_{reason}"
        )
        assert (
            result.kind == ("failed" if read_only else "unknown") and result.error_code == expected
        )
        assert result.output is None and result.artifact_sha256 is None
        event = actions.events(route.plan.execution.plan_id)[-1]
        assert (
            event.error_code == expected
            and event.output_sha256 is None
            and event.artifact_sha256 is None
        )
        assert executor.calls == 1 and executor.reconciliations == (stage == "reconcile")
        assert all(operation.state == "completed" for operation in audit.operations())
        assert (root / "file.txt").read_text(encoding="utf-8") == "unchanged"
    finally:
        plans.close()
        audit.close()
