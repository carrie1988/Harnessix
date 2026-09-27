"""后置归一/摘要期限、取消记账及真实文件效果的只对账恢复。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, EffectClass, RiskLevel
from harnessix.trusted_actions import operation_router, outcome_validation, output_budget
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.trusted_actions.test_router import (
    FakeExecutor,
    binding,
    context,
    definition,
    invocation,
    router,
)


def setup(root: Path, executor, read_only=False):
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    tool = binding(
        effect=EffectClass.READ_ONLY if read_only else EffectClass.NON_IDEMPOTENT_WRITE,
        risk=RiskLevel.LOW if read_only else RiskLevel.HIGH,
        recovery="none" if read_only else "durable_ledger",
    )
    actions, plans, audit = router(root, definition(tool, executor))
    route = actions.plan(invocation(tool), context(root))
    if not read_only:
        actions.decide(
            route.plan.execution.plan_id,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="lifecycle-test"),
        )
    return actions, plans, audit, route.plan.execution.plan_id


@pytest.mark.parametrize(
    "stage,read_only", [("execute", True), ("execute", False), ("reconcile", False)]
)
@pytest.mark.parametrize("boundary", ["normalize", "digest"])
async def test_post_return_work_cannot_outlive_processing_deadline(
    tmp_path, monkeypatch, stage, read_only, boundary
):
    clock = [100.0]
    for module in (operation_router, outcome_validation, output_budget):
        monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output={"ok": True}))
    actions, plans, audit, plan_id = setup(tmp_path / "workspace", executor, read_only)
    try:
        if stage == "reconcile":
            executor.result = ActionExecutionOutcome(kind="unknown", error_code="executor_error")
            await actions.execute(plan_id)
            executor.reconciled = ActionExecutionOutcome(kind="succeeded", output={"ok": True})
        original = getattr(
            operation_router,
            "normalize_failure_outcome" if boundary == "normalize" else "canonical_digest",
        )

        def expired(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] += 20  # 确定性跨越10秒后置期限，不耗费真实时间。
            return result

        monkeypatch.setattr(
            operation_router,
            "normalize_failure_outcome" if boundary == "normalize" else "canonical_digest",
            expired,
        )
        result = await (actions.reconcile if stage == "reconcile" else actions.execute)(plan_id)
        expected = (
            "executor_output_timeout"
            if read_only
            else "write_output_timeout_unknown"
            if stage == "execute"
            else "reconciliation_output_timeout"
        )
        assert result.error_code == expected and result.output is None
        assert actions.events(plan_id)[-1].output_sha256 is None
        assert all(operation.state == "completed" for operation in audit.operations())
    finally:
        plans.close()
        audit.close()


class DurableWriter:
    """真实写入一次有界文件，Owner事实从文件重读；原始大返回值不进入公开面。"""

    def __init__(self, root):
        self.root, self.calls, self.reconciliations = root, 0, 0

    async def execute(self, plan, arguments):
        self.calls += 1
        with (self.root / "file.txt").open("ab") as stream:
            stream.write(b"effect\n")
            stream.flush()
            os.fsync(stream.fileno())
        return ActionExecutionOutcome(kind="succeeded", output="x" * (1024 * 1024 + 1))

    async def reconcile(self, plan, arguments):
        self.reconciliations += 1
        assert (self.root / "file.txt").read_bytes() == b"beforeeffect\n"
        return ActionExecutionOutcome(kind="succeeded", output={"confirmed": True})


async def test_written_effect_is_not_reexecuted_after_output_rejection_and_store_reopen(tmp_path):
    root = tmp_path / "workspace"
    executor = DurableWriter(root)
    actions, plans, audit, plan_id = setup(root, executor)
    rejected = await actions.execute(plan_id)
    assert rejected.kind == "unknown" and rejected.error_code == "write_output_limit_unknown"
    assert (root / "file.txt").read_bytes() == b"beforeeffect\n"
    plans.close()
    audit.close()
    tool = binding(
        effect=EffectClass.NON_IDEMPOTENT_WRITE, risk=RiskLevel.HIGH, recovery="durable_ledger"
    )
    recovered, plans, audit = router(root, definition(tool, executor))
    try:
        result = await recovered.reconcile(plan_id)
        assert result.kind == "succeeded" and result.output == {"confirmed": True}
        assert executor.calls == 1 and executor.reconciliations == 1
        assert [operation.state for operation in audit.operations()] == ["completed", "completed"]
        assert (root / "file.txt").read_bytes() == b"beforeeffect\n"
        with pytest.raises(KernelError) as caught:
            await recovered.execute(plan_id)
        assert caught.value.code == "execution_plan_stale" and executor.calls == 1
    finally:
        plans.close()
        audit.close()


@pytest.mark.parametrize(
    "stage,read_only", [("execute", True), ("execute", False), ("reconcile", False)]
)
async def test_parent_cancel_immediately_after_return_is_persisted_and_propagated(
    tmp_path, stage, read_only
):
    class CancelOnReturn(FakeExecutor):
        async def execute(self, plan, arguments):
            self.calls += 1
            if stage == "execute":
                asyncio.current_task().cancel()
                return ActionExecutionOutcome(kind="succeeded", output={"ok": True})
            return ActionExecutionOutcome(kind="unknown", error_code="executor_error")

        async def reconcile(self, plan, arguments):
            self.reconciliations += 1
            asyncio.current_task().cancel()
            return ActionExecutionOutcome(kind="succeeded", output={"ok": True})

    executor = CancelOnReturn(ActionExecutionOutcome(kind="succeeded"))
    actions, plans, audit, plan_id = setup(tmp_path / "workspace", executor, read_only)
    try:
        if stage == "reconcile":
            await actions.execute(plan_id)
        task = asyncio.create_task(
            (actions.reconcile if stage == "reconcile" else actions.execute)(plan_id)
        )
        with pytest.raises(asyncio.CancelledError):
            await task
        expected = (
            "executor_cancelled"
            if read_only
            else "cancelled_write_effect_unknown"
            if stage == "execute"
            else "reconciliation_cancelled"
        )
        event = actions.events(plan_id)[-1]
        assert event.error_code == expected and event.output_sha256 is None
        assert actions.status(plan_id).state == ("failed" if read_only else "unknown")
        assert all(operation.state == "completed" for operation in audit.operations())
    finally:
        plans.close()
        audit.close()


def _crash_before_budget_result_completion(root_string, plan_string):
    """独立子进程已写入效果并拒绝大返回值，在Audit Complete之前真实退出。"""
    from uuid import UUID

    root = Path(root_string)
    tool = binding(
        effect=EffectClass.NON_IDEMPOTENT_WRITE, risk=RiskLevel.HIGH, recovery="durable_ledger"
    )
    actions, plans, audit = router(root, definition(tool, DurableWriter(root)))

    def crash(*args, **kwargs):
        assert kwargs["error_code"] == "write_output_limit_unknown"
        assert kwargs["output_sha256"] is None and kwargs["target"] == "unknown"
        os._exit(73)

    audit.complete_operation = crash
    asyncio.run(actions.execute(UUID(plan_string)))


async def test_real_process_exit_after_rejection_recovers_by_owner_without_reexecution(tmp_path):
    import sys

    root = tmp_path / "workspace"
    executor = DurableWriter(root)
    actions, plans, audit, plan_id = setup(root, executor)
    plans.close()
    audit.close()
    script = (
        "import sys; "
        "from tests.trusted_actions.test_executor_output_lifecycle "
        "import _crash_before_budget_result_completion; "
        "_crash_before_budget_result_completion(*sys.argv[1:])"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(root),
        str(plan_id),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 73, stderr.decode(errors="replace")
    assert (root / "file.txt").read_bytes() == b"beforeeffect\n"
    tool = binding(
        effect=EffectClass.NON_IDEMPOTENT_WRITE, risk=RiskLevel.HIGH, recovery="durable_ledger"
    )
    recovered, plans, audit = router(root, definition(tool, executor))
    try:
        assert recovered.status(plan_id).state == "running"
        assert [item.state for item in audit.operations()] == ["active"]
        assert recovered.recover_interrupted() == (plan_id,)
        assert recovered.status(plan_id).state == "unknown"
        confirmed = await recovered.reconcile(plan_id)
        assert confirmed.kind == "succeeded" and confirmed.output == {"confirmed": True}
        assert [item.state for item in audit.operations()] == ["interrupted", "completed"]
        assert executor.calls == 0 and executor.reconciliations == 1
        assert (root / "file.txt").read_bytes() == b"beforeeffect\n"
    finally:
        plans.close()
        audit.close()
