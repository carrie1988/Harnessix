"""v2诊断正文必须同时满足闭合DTO、原计划身份及原审计Hash。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.trusted_actions.agent_gateway_output import terminal_result
from tests.trusted_actions.test_agent_gateway import FixedOutput, agent_state
from tests.trusted_actions.test_process_failure_projection import ProcessExecutor, process_route
from tests.trusted_actions.test_success_projection_boundaries import output_reference


@pytest.mark.parametrize("delivery", ["owner", "inline"])
@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize(
    "case", ["valid", "missing", "size", "limit", "control", "extra", "truncated", "v1"]
)
async def test_preview_requires_formal_contract_even_with_matching_audit(
    tmp_path, case, recovery, delivery
):
    class Returning(ProcessExecutor):
        async def execute(self, plan, arguments):
            raw = await super().execute(plan, arguments)
            public = dict(raw.output)
            public["version"] = "trusted-process-output/v2"
            public["diagnostic_preview"] = {
                "stdout": {"text": "test diagnostic\n", "size_bytes": 16, "truncated": False},
                "stderr": {"text": "", "size_bytes": 0, "truncated": False},
            }
            preview = public["diagnostic_preview"]["stdout"]
            if case == "missing":
                del public["diagnostic_preview"]
            elif case == "size":
                preview["size_bytes"] = 17
            elif case == "limit":
                preview.update(text="x" * 1025, size_bytes=1025)
            elif case == "control":
                preview["text"] = "x" * 15 + "\x00"
            elif case == "extra":
                public["diagnostic_preview"]["private"] = "not authorized"
            elif case == "truncated":
                preview["truncated"] = True
            elif case == "v1":
                public["version"] = "trusted-process-output/v1"
            return raw.model_copy(update={"output": public})

    executor = Returning(False, "exited", "exited", 0)
    root = tmp_path / "workspace"
    actions, plans, audit, initial = process_route(root, executor)
    try:
        outcome = await actions.execute(initial.plan.execution.plan_id)
        route = actions.status(initial.plan.execution.plan_id)
        projected = {**outcome.output, "artifact": output_reference(outcome.artifact_sha256)}
        output = FixedOutput(projected)
        state = SimpleNamespace(
            router=actions, outputs={route.plan.binding.tool: output} if delivery == "owner" else {}
        )
        thread, turn, call = agent_state(root)
        call = call.model_copy(update={"tool": route.plan.binding.tool})
        original_events = actions.events(route.plan.execution.plan_id)
        current = (
            outcome.model_copy(update={"output": None})
            if recovery and delivery == "owner"
            else outcome
        )
        if case == "valid":
            result = await terminal_result(
                state,
                route,
                thread,
                turn,
                call,
                current,
                CancelToken(),
                origin="recovery" if recovery else "execution",
            )
            assert result.outcome == "succeeded"
            assert result.output == (projected if delivery == "owner" else outcome.output)
        else:
            with pytest.raises(KernelError) as caught:
                await terminal_result(
                    state,
                    route,
                    thread,
                    turn,
                    call,
                    current,
                    CancelToken(),
                    origin="recovery" if recovery else "execution",
                )
            assert caught.value.code == "trusted_action_output_mismatch"
        assert actions.events(route.plan.execution.plan_id) == original_events
        assert executor.calls == 1
    finally:
        plans.close()
        audit.close()
