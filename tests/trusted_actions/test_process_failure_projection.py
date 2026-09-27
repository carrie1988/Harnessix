"""正式Process/Eval摘要的失败合同、Provider正常返回及恢复投影回归。"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import BaseModel

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    RiskLevel,
    utc_now,
)
from harnessix.execution.contracts import canonical_digest
from harnessix.processes.test_contracts import RunTestsInput
from harnessix.processes.trusted_output import (
    build_trusted_process_output,
    trusted_process_public_output,
)
from harnessix.product_config.process_action import RunProfileInput
from harnessix.trusted_actions.agent_gateway_output import terminal_result
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    CodingActionInvocation,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.public_outcomes import normalize_failure_outcome
from harnessix.trusted_actions.router import (
    ResolvedAction,
    TrustedActionDefinition,
    canonical_action_resource,
)
from tests.processes.test_trusted_output import lease
from tests.trusted_actions.test_agent_gateway import FixedOutput, agent_state
from tests.trusted_actions.test_public_error_leakage import _payload
from tests.trusted_actions.test_router import context, router


class ProcessExecutor:
    """生成真实Lease文档形状，身份由本次冻结计划确定。"""

    def __init__(self, evaluation: bool, state: str, stop: str, returncode: int | None) -> None:
        self.evaluation, self.state, self.stop, self.returncode = (
            evaluation,
            state,
            stop,
            returncode,
        )
        self.calls = 0
        self.outcome = None

    async def execute(self, plan, _arguments: BaseModel) -> ActionExecutionOutcome:
        self.calls += 1
        observed = lease(b"test diagnostic\n", b"", returncode=1).model_copy(
            update={
                "process_id": plan.execution.plan_id,
                "plan_id": plan.execution.plan_id,
                "state": self.state,
                "stop_reason": self.stop,
                "returncode": self.returncode,
            }
        )
        if self.state == "failed":
            observed = observed.model_copy(
                update={"owner_identity": None, "pid": None, "started_at": None}
            )
        document = build_trusted_process_output("unit-tests", observed, b"test diagnostic\n", b"")
        if self.state == "unknown":
            kind, code = "unknown", "process_state_unknown"
        elif self.state == "failed":
            kind, code = "failed", "process_launch_failed"
        elif self.stop == "exited" and self.evaluation:
            kind, code = "succeeded", None
        else:
            kind, code = (
                "failed",
                {
                    "exited": "process_nonzero_exit",
                    "timeout": "process_timeout",
                    "cancelled": "process_cancelled",
                }[self.stop],
            )
        self.outcome = ActionExecutionOutcome(
            kind=kind,
            error_code=code,
            output=trusted_process_public_output(document, include_passed=self.evaluation),
            artifact_sha256=hashlib.sha256(document.to_jsonl()).hexdigest(),
        )
        return self.outcome


def process_route(root: Path, executor: ProcessExecutor):
    root.mkdir()
    model = RunTestsInput if executor.evaluation else RunProfileInput
    name = "run_tests" if executor.evaluation else "run_profile.unit-tests"
    binding = build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix.product",
        tool=name,
        tool_version="1",
        tool_fingerprint=canonical_digest(name),
        input_schema_sha256=canonical_digest(model.model_json_schema()),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        recovery_mode="durable_ledger",
        executor_id="eval.run-tests"
        if executor.evaluation
        else "product.process-profile.unit-tests",
    )
    definition = TrustedActionDefinition(
        binding,
        model,
        lambda *_: ResolvedAction(
            resources=(
                canonical_action_resource(
                    kind="process", access="execute", identifier={"profile": "unit-tests"}
                ),
            )
        ),
        executor,
    )
    actions, plans, audit = router(root, definition)
    invocation = CodingActionInvocation(
        invocation_id=uuid4(),
        source=binding.source,
        source_id=binding.source_id,
        tool=name,
        tool_version="1",
        tool_fingerprint=binding.tool_fingerprint,
        arguments={"profile": "unit-tests"},
        idempotency_key="process-failure",
    )
    route = actions.plan(invocation, context(root))
    actions.decide(
        route.plan.execution.plan_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="failure-test"),
    )
    return actions, plans, audit, route


@pytest.mark.parametrize("evaluation", [False, True])
@pytest.mark.parametrize(
    "state,stop,returncode",
    [
        ("exited", "exited", 1),
        ("exited", "timeout", -15),
        ("exited", "cancelled", -15),
        ("failed", "launch_failed", None),
        ("unknown", "host_lost", None),
    ],
)
async def test_formal_failure_metadata_and_eval_business_result_are_preserved(
    tmp_path, evaluation, state, stop, returncode
):
    executor = ProcessExecutor(evaluation, state, stop, returncode)
    actions, plans, audit, route = process_route(tmp_path / "workspace", executor)
    try:
        outcome = await actions.execute(route.plan.execution.plan_id)
        assert outcome == executor.outcome and outcome.output is not None
        event = actions.events(route.plan.execution.plan_id)[-1]
        assert event.output_sha256 == canonical_digest(outcome.output)
        assert event.artifact_sha256 == outcome.artifact_sha256
        assert actions.status(route.plan.execution.plan_id).state == outcome.kind
        if evaluation and stop == "exited":
            assert outcome.kind == "succeeded" and outcome.output["passed"] is False
        assert executor.calls == 1
    finally:
        plans.close()
        audit.close()


MUTATIONS = [
    "extra",
    "profile",
    "process_id",
    "stop_reason",
    "returncode",
    "stream_text",
    "stream_size",
    "stream_bool",
    "complete",
    "passed",
    "missing_artifact",
    "wrong_kind",
    "wrong_code",
]


def mutate(outcome, case):
    output = outcome.model_copy(deep=True).output
    assert isinstance(output, dict)
    fields = {"output": output}
    if case == "extra":
        output["diagnostic"] = _payload()
    elif case == "profile":
        output["profile"] = "different"
    elif case == "process_id":
        output["process_id"] = str(uuid4())
    elif case == "stop_reason":
        output["stop_reason"] = "arbitrary_diagnostic"
    elif case == "returncode":
        output["returncode"] = None
    elif case == "stream_text":
        output["stderr"]["text"] = _payload()
    elif case == "stream_size":
        output["stdout"]["persisted_bytes"] = 65 * 1024 * 1024
    elif case == "stream_bool":
        output["stdout"]["eof"] = "true"
    elif case == "complete":
        output["stderr"]["eof"] = False
    elif case == "passed":
        output["passed"] = True
    elif case == "missing_artifact":
        fields["artifact_sha256"] = None
    elif case == "wrong_kind":
        fields["kind"] = "unknown"
    elif case == "wrong_code":
        fields["error_code"] = "process_launch_failed"
    return outcome.model_copy(update=fields)


@pytest.mark.parametrize("case", MUTATIONS)
@pytest.mark.parametrize("evaluation", [False, True])
async def test_registered_code_does_not_allow_arbitrary_process_failure_body(
    tmp_path, evaluation, case
):
    executor = ProcessExecutor(evaluation, "exited", "timeout", -15)
    actions, plans, audit, route = process_route(tmp_path / "workspace", executor)
    try:
        raw = await executor.execute(route.plan, RunTestsInput(profile="unit-tests"))
        hostile = mutate(raw, case)
        safe = normalize_failure_outcome(route.plan, hostile, stage="execute")
        assert safe.kind == hostile.kind and safe.error_code == "action_failure_output_invalid"
        assert safe.output is None and safe.artifact_sha256 is None
    finally:
        plans.close()
        audit.close()


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "extra",
        "profile",
        "process_id",
        "stream_text",
        "wrong_hash",
        "wrong_artifact",
        "missing_artifact",
        "dict_error",
        "dict_kernel_error",
    ],
)
@pytest.mark.parametrize("recovery", [False, True])
async def test_returned_failure_projection_is_validated_without_reexecution(
    tmp_path, case, recovery
):
    executor = ProcessExecutor(False, "exited", "timeout", -15)
    root = tmp_path / "workspace"
    actions, plans, audit, route = process_route(root, executor)
    try:
        outcome = await actions.execute(route.plan.execution.plan_id)
        route = actions.status(route.plan.execution.plan_id)
        events_before = actions.events(route.plan.execution.plan_id)
        reference = ArtifactRef(
            artifact_id=uuid4(),
            sha256=outcome.artifact_sha256,
            size_bytes=1024,
            records=2,
            complete=True,
            expires_at=utc_now() + timedelta(days=1),
        )
        if case in {"extra", "profile", "process_id", "stream_text"}:
            output = mutate(outcome, case).output
        else:
            output = outcome.model_copy(deep=True).output
        if case == "wrong_hash":
            output["stdout"]["observed_sha256"] = "0" * 64
        output["artifact"] = reference.model_dump(mode="json")
        if case == "wrong_artifact":
            output["artifact"]["sha256"] = "0" * 64
        elif case == "missing_artifact":
            del output["artifact"]
        if case in {"dict_error", "dict_kernel_error"}:

            class FaultMapping(dict):
                def items(self):
                    if case == "dict_kernel_error":
                        raise KernelError("secret_in_code_canary", _payload())
                    raise RuntimeError(_payload())

            output = FaultMapping(output)
        provider = FixedOutput(output)
        state = SimpleNamespace(router=actions, outputs={route.plan.binding.tool: provider})
        thread, turn, call = agent_state(root)
        call = call.model_copy(update={"tool": route.plan.binding.tool})
        if recovery:
            outcome = outcome.model_copy(update={"output": None})
        if case == "valid":
            result = await terminal_result(
                state,
                route,
                thread,
                turn,
                call,
                outcome,
                CancelToken(),
                origin="recovery" if recovery else "execution",
            )
            assert result.output == output and result.error.code == "process_timeout"
        else:
            with pytest.raises(KernelError) as caught:
                await terminal_result(
                    state,
                    route,
                    thread,
                    turn,
                    call,
                    outcome,
                    CancelToken(),
                    origin="recovery" if recovery else "execution",
                )
            assert caught.value.code == "trusted_action_output_mismatch"
        assert provider.calls == 1 and executor.calls == 1
        assert actions.events(route.plan.execution.plan_id) == events_before
    finally:
        plans.close()
        audit.close()
