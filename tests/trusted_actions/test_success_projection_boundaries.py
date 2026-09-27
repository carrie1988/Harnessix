"""成功Owner投影同样必须绑定Audit；业务成功不能授权追加未审计正文。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, utc_now
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.trusted_actions.test_agent_gateway import (
    FakeExecutor,
    FixedOutput,
    agent_state,
    build_gateway,
)
from tests.trusted_actions.test_public_error_leakage import _assert_no_leak, _payload


def output_reference(sha256: str):
    return ArtifactRef(
        artifact_id=uuid4(),
        sha256=sha256,
        size_bytes=1024,
        records=2,
        complete=True,
        expires_at=utc_now() + timedelta(days=1),
    ).model_dump(mode="json")


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        "added_diagnostic",
        "changed_summary",
        "wrong_artifact",
        "missing_artifact",
        "incomplete_ref",
        "scalar",
        "extra_ref",
    ],
)
async def test_success_projection_rejects_content_not_bound_to_audit(
    tmp_path: Path, recovery: bool, case: str
):
    root = tmp_path / "workspace"
    root.mkdir()
    summary = {"summary": "completed"}
    projected = {**summary, "artifact": output_reference("b" * 64)}
    if case == "added_diagnostic":
        projected["diagnostic"] = _payload()
    elif case == "changed_summary":
        projected["summary"] = "different"
    elif case == "wrong_artifact":
        projected["artifact"]["sha256"] = "c" * 64
    elif case == "missing_artifact":
        del projected["artifact"]
    elif case == "incomplete_ref":
        projected["artifact"] = {"sha256": "b" * 64}
    elif case == "scalar":
        projected = _payload()
    elif case == "extra_ref":
        projected["artifact"]["debug"] = _payload()
    output = FixedOutput(projected)
    executor = FakeExecutor(
        ActionExecutionOutcome(kind="succeeded", output=summary, artifact_sha256="b" * 64)
    )
    gateway, actions, plans, audit = build_gateway(
        root, executor, presentation="process", output=output
    )
    try:
        thread, turn, call = agent_state(root)
        request = await gateway.prepare(thread, turn, call, CancelToken())
        approved = gateway.decide(
            thread,
            turn,
            call,
            request,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="projection-test"),
        )
        if recovery:
            await actions.execute(request.plan_id)
            before = actions.events(request.plan_id)
            with pytest.raises(KernelError) as caught:
                await gateway.recover(thread, turn, call, approved, CancelToken())
            assert actions.events(request.plan_id) == before
        else:
            with pytest.raises(KernelError) as caught:
                await gateway.execute(thread, turn, call, approved, CancelToken())
        assert caught.value.code == "trusted_action_output_mismatch"
        assert actions.status(request.plan_id).state == "succeeded"
        assert actions.events(request.plan_id)[-1].error_code is None
        assert executor.calls == 1 and executor.reconciliations == 0 and output.calls == 1
        _assert_no_leak(str(caught.value), repr(caught.value))
    finally:
        gateway.close()
        plans.close()
        audit.close()
