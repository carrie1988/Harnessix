"""正式Secret绑定的公开值回归：效果已确认时拒绝正文，不重执行或调用Owner。"""

from __future__ import annotations

import base64
from dataclasses import replace
from urllib.parse import quote

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.execution.contracts import SecretVersionBinding, canonical_digest
from harnessix.secrets.provider import (
    EnvironmentSecretProvider,
    EnvironmentSecretSource,
    resolve_secret_environment,
)
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.agent_gateway_output import terminal_result
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from harnessix.trusted_actions.router import ResolvedAction, canonical_action_resource
from tests.trusted_actions.test_agent_gateway import (
    FakeExecutor,
    FixedOutput,
    agent_state,
    build_gateway,
    descriptor,
    runtime_context,
)
from tests.trusted_actions.test_success_projection_boundaries import output_reference

CANARY = "scope-original-value/+for-public-7"
BINDING = SecretVersionBinding(name="registry", version="7", target="TOKEN")


def source(version="7"):
    return EnvironmentSecretProvider(
        (EnvironmentSecretSource("registry", version, "PROTECTED_VALUE"),),
        environment={"PROTECTED_VALUE": CANARY},
    )


class ScopedExecutor(FakeExecutor):
    def __init__(self, body, provider, artifact=None):
        super().__init__(ActionExecutionOutcome(kind="succeeded"))
        self.body, self.provider, self.artifact = body, provider, artifact

    async def execute(self, plan, arguments):
        with resolve_secret_environment(
            plan.execution.secrets, self.provider, platform=plan.execution.workspace.platform
        ) as scope:
            assert scope.as_text()["TOKEN"] == CANARY
        self.outcome = ActionExecutionOutcome(
            kind="succeeded", output=self.body, artifact_sha256=self.artifact
        )
        return await super().execute(plan, arguments)


def secret_gateway(root, body, *, scope=None, owner=None):
    root.mkdir()
    (root / "file.txt").write_text("unchanged")
    executor = ScopedExecutor(body, scope if scope is not None else source())
    old, router, plans, audit = build_gateway(root, executor)
    key = next(iter(router._definitions))
    definition = router._definitions[key]

    def resolve(arguments, context):
        resolved = definition.resolve(arguments, context)
        return ResolvedAction(
            resources=resolved.resources
            + (
                canonical_action_resource(
                    kind="secret", access="use", identifier=BINDING.model_dump(mode="json")
                ),
            ),
            workspace_resources=resolved.workspace_resources,
        )

    router._definitions[key] = replace(definition, resolve=resolve)
    context = replace(runtime_context(root), secrets=(BINDING,))
    options = {} if scope is None else {"secret_scope": scope}
    gate = RouterBackedAgentActionGateway(
        router,
        (descriptor(),),
        lambda *_: context,
        presentations={descriptor().name: "process"} if owner else None,
        outputs={descriptor().name: owner} if owner else None,
        **options,
    )
    return gate, router, plans, audit, executor


async def approved(gate, root):
    thread, turn, call = agent_state(root)
    request = await gate.prepare(thread, turn, call, CancelToken())
    decision = gate.decide(
        thread,
        turn,
        call,
        request,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="test"),
    )
    return thread, turn, call, decision


@pytest.mark.parametrize("encoding", ["raw", "base64", "url", "hex"])
@pytest.mark.parametrize("protected", [False, True])
async def test_declared_secret_is_not_authorized_by_matching_field_and_hash(
    tmp_path, encoding, protected
):
    encoded = {
        "raw": CANARY,
        "base64": base64.b64encode(CANARY.encode()).decode(),
        "url": quote(CANARY, safe=""),
        "hex": CANARY.encode().hex(),
    }[encoding]
    scope = SecretPublicationScope((BINDING,), source()) if protected else None
    root = tmp_path / "workspace"
    gate, router, plans, audit, executor = secret_gateway(root, {"summary": encoded}, scope=scope)
    try:
        thread, turn, call, decision = await approved(gate, root)
        with pytest.raises(KernelError) as caught:
            await gate.execute(thread, turn, call, decision, CancelToken())
        assert caught.value.code == (
            "trusted_action_secret_leak" if protected else "trusted_action_secret_unavailable"
        )
        assert encoded not in str(caught.value) and CANARY not in str(caught.value)
        route = router.status(decision.plan_id)
        assert route.state == "succeeded" and route.plan.execution.secrets == (BINDING,)
        assert router.events(decision.plan_id)[-1].output_sha256 == canonical_digest(
            {"summary": encoded}
        )
        assert executor.calls == 1 and executor.reconciliations == 0
        assert (root / "file.txt").read_text() == "unchanged"
    finally:
        gate.close()
        plans.close()
        audit.close()
        if scope:
            scope.close()


@pytest.mark.parametrize("case", ["safe", "raw-leak", "recovery"])
async def test_secret_owner_prepublication_and_hash_only_restore(tmp_path, case):
    root = tmp_path / "workspace"
    scope = SecretPublicationScope((BINDING,), source())
    body = {"summary": CANARY if case == "raw-leak" else "completed"}
    reference = output_reference("4" * 64)
    owner = FixedOutput({**body, "artifact": reference})
    gate, router, plans, audit, executor = secret_gateway(root, body, scope=scope, owner=owner)
    executor.artifact = reference["sha256"]
    try:
        thread, turn, call, decision = await approved(gate, root)
        if case == "recovery":
            outcome = await router.execute(decision.plan_id)
            before = router.events(decision.plan_id)
            result = await terminal_result(
                gate._state,
                router.status(decision.plan_id),
                thread,
                turn,
                call,
                outcome.model_copy(update={"output": None}),
                CancelToken(),
                origin="recovery",
                approval=decision,
                descriptor=descriptor(),
            )
            assert result.output is None and result.trusted_action.state == "succeeded"
            assert router.events(decision.plan_id) == before and owner.calls == 0
        elif case == "raw-leak":
            with pytest.raises(KernelError) as caught:
                await gate.execute(thread, turn, call, decision, CancelToken())
            assert caught.value.code == "trusted_action_secret_leak" and owner.calls == 0
        else:
            result = await gate.execute(thread, turn, call, decision, CancelToken())
            assert result.output == owner.projected and owner.calls == 1
        assert router.status(decision.plan_id).state == "succeeded"
        assert executor.calls == 1 and executor.reconciliations == 0
    finally:
        gate.close()
        plans.close()
        audit.close()
        scope.close()


@pytest.mark.parametrize("mode", ["timeout", "token", "parent"])
async def test_secret_scan_is_inside_deadline_and_cancel_before_owner(tmp_path, monkeypatch, mode):
    import asyncio

    from harnessix.agent.cancellation import TurnCancelled
    from harnessix.secrets import publication
    from harnessix.trusted_actions import agent_gateway_output, output_budget

    root = tmp_path / "workspace"
    scope = SecretPublicationScope((BINDING,), source())
    owner = FixedOutput({"summary": "completed", "artifact": output_reference("4" * 64)})
    gate, router, plans, audit, executor = secret_gateway(
        root, {"summary": "completed"}, scope=scope, owner=owner
    )
    executor.artifact = "4" * 64
    token = CancelToken()
    clock = [agent_gateway_output.monotonic()]
    original = publication._ScanBudget.step

    def step(work, depth):
        if mode == "timeout":
            clock[0] += 20
        elif mode == "token":
            token.cancel()
        else:
            asyncio.current_task().cancel()
        original(work, depth)

    monkeypatch.setattr(publication._ScanBudget, "step", step)
    monkeypatch.setattr(agent_gateway_output, "monotonic", lambda: clock[0])
    monkeypatch.setattr(output_budget, "monotonic", lambda: clock[0])
    try:
        thread, turn, call, decision = await approved(gate, root)
        expected = (
            KernelError
            if mode == "timeout"
            else TurnCancelled
            if mode == "token"
            else asyncio.CancelledError
        )
        with pytest.raises(expected) as caught:
            await asyncio.create_task(gate.execute(thread, turn, call, decision, token))
        if mode == "timeout":
            assert caught.value.code == "trusted_action_output_timeout"
        assert owner.calls == 0 and router.status(decision.plan_id).state == "succeeded"
        assert executor.calls == 1 and executor.reconciliations == 0
    finally:
        gate.close()
        plans.close()
        audit.close()
        scope.close()
