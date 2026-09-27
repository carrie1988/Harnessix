"""Process/Eval成功投影的真实Lease形状、正式DTO与计划/业务语义验证。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.trusted_actions.agent_gateway_output import terminal_result
from tests.trusted_actions.test_agent_gateway import FixedOutput, agent_state
from tests.trusted_actions.test_process_failure_projection import ProcessExecutor, process_route
from tests.trusted_actions.test_success_projection_boundaries import output_reference


@pytest.mark.parametrize("evaluation,returncode", [(False, 0), (True, 0), (True, 1)])
@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize(
    "case", ["valid", "profile", "process_id", "extra", "state", "stop", "returncode", "passed"]
)
async def test_success_process_summary_requires_formal_semantics_even_when_audit_hash_matches(
    tmp_path, evaluation, returncode, recovery, case
):
    class Returning(ProcessExecutor):
        async def execute(self, plan, arguments):
            raw = await super().execute(plan, arguments)
            summary = dict(raw.output)
            if case == "profile":
                summary["profile"] = "different"
            elif case == "process_id":
                summary["process_id"] = str(uuid4())
            elif case == "extra":
                summary["extra"] = "diagnostic"
            elif case == "state":
                summary["state"], summary["returncode"], summary["stop_reason"] = (
                    "unknown",
                    None,
                    "host_lost",
                )
            elif case == "stop":
                summary["stop_reason"] = "timeout"
            elif case == "returncode":
                summary["returncode"] = None
            elif case == "passed":
                summary["passed"] = not (returncode == 0)
            return raw.model_copy(update={"output": summary})

    executor = Returning(evaluation, "exited", "exited", returncode)
    root = tmp_path / "workspace"
    actions, plans, audit, initial = process_route(root, executor)
    try:
        outcome = await actions.execute(initial.plan.execution.plan_id)
        summary = outcome.output
        assert actions.events(initial.plan.execution.plan_id)[-1].output_sha256 == canonical_digest(
            summary
        )
        route = actions.status(initial.plan.execution.plan_id)
        projected = {**summary, "artifact": output_reference(outcome.artifact_sha256)}
        output = FixedOutput(projected)
        state = SimpleNamespace(router=actions, outputs={route.plan.binding.tool: output})
        thread, turn, call = agent_state(root)
        call = call.model_copy(update={"tool": route.plan.binding.tool})
        before = actions.events(route.plan.execution.plan_id)
        if case == "valid":
            result = await terminal_result(
                state,
                route,
                thread,
                turn,
                call,
                outcome.model_copy(update={"output": None}) if recovery else outcome,
                CancelToken(),
                origin="recovery" if recovery else "execution",
            )
            assert result.output == projected and result.outcome == "succeeded"
            if evaluation:
                assert result.output["passed"] is (returncode == 0)
        else:
            with pytest.raises(KernelError) as caught:
                await terminal_result(
                    state,
                    route,
                    thread,
                    turn,
                    call,
                    outcome.model_copy(update={"output": None}) if recovery else outcome,
                    CancelToken(),
                    origin="recovery" if recovery else "execution",
                )
            assert caught.value.code == "trusted_action_output_mismatch"
        assert actions.events(route.plan.execution.plan_id) == before
        assert executor.calls == 1 and output.calls == 1
    finally:
        plans.close()
        audit.close()
