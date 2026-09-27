"""公开保护拒绝与已确认效果分离；终态重投影来源不等于本次是否允许恢复元数据。"""

from __future__ import annotations

import pytest

from harnessix.agent.errors import KernelError
from harnessix.trusted_actions.public_errors import (
    PUBLIC_OUTPUT_REJECTIONS,
    sanitize_gateway_exception,
)
from tests.agent.test_publication import CANARY
from tests.trusted_actions.test_gateway_error_boundaries import fault_gateway, invoke_callback


@pytest.mark.parametrize("code", sorted(PUBLIC_OUTPUT_REJECTIONS))
@pytest.mark.parametrize("recovery", [False, True])
async def test_public_rejection_keeps_fixed_code_or_recovers_only_verified_metadata(
    tmp_path, code, recovery
):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before")
    gateway, actions, plans, audit, executor, callbacks = fault_gateway(
        root, "output", KernelError(code, CANARY)
    )
    try:
        if recovery:
            result = await invoke_callback(gateway, actions, root, "output", recovery=True)
            assert result.outcome == "succeeded" and result.output is None
            assert result.trusted_action.state == "succeeded"
            assert result.trusted_action.artifact_sha256 is None
            # Router已终态时保持原execution来源，不据此误判为允许重新发布正文。
            assert result.trusted_action.origin == "execution"
            assert CANARY not in result.model_dump_json()
        else:
            with pytest.raises(KernelError) as caught:
                await invoke_callback(gateway, actions, root, "output", recovery=False)
            assert caught.value.code == code and CANARY not in str(caught.value)
        assert callbacks.route is not None
        assert actions.status(callbacks.route.plan.execution.plan_id).state == "succeeded"
        assert executor.calls == 1 and executor.reconciliations == 0
        assert callbacks.calls == 1
        assert all(
            CANARY not in item.model_dump_json()
            for item in actions.events(callbacks.route.plan.execution.plan_id)
        )
    finally:
        gateway.close()
        plans.close()
        audit.close()


@pytest.mark.parametrize("stage", ["context", "review"])
@pytest.mark.parametrize("code", sorted(PUBLIC_OUTPUT_REJECTIONS))
def test_public_output_codes_do_not_escape_their_callback_stage(stage, code):
    sanitized = sanitize_gateway_exception(KernelError(code, CANARY), stage=stage)
    assert sanitized.code == f"trusted_action_{stage}_failed"
    assert CANARY not in str(sanitized)
