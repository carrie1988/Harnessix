"""正式来源的成功正文不能仅凭合法JSON及匹配Hash获得公开权限。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import BaseModel

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    RiskLevel,
    utc_now,
)
from harnessix.execution.contracts import canonical_digest
from harnessix.mcp.schema import McpToolArguments
from harnessix.trusted_actions.agent_gateway_output import terminal_result
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    CodingActionInvocation,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ResolvedAction,
    TrustedActionDefinition,
    canonical_action_resource,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from tests.trusted_actions.test_agent_gateway import FixedOutput, agent_state
from tests.trusted_actions.test_public_error_leakage import _assert_no_leak, _payload
from tests.trusted_actions.test_router import context, router
from tests.trusted_actions.test_success_projection_boundaries import output_reference

FAMILIES = ["patch", "git", "skill_load", "skill_resource", "mcp"]


def family_data(family):
    """夹具只代表合同边界；实际Delivery、Skill和MCP Owner另由既有集成验证。"""
    source, tool, executor = "builtin", "apply_patch_batch", "product.workspace-patch"
    arguments = {"files": [{"path": "file.txt"}]}
    if family == "git":
        tool, executor = "git.push", "delivery.git-push"
        arguments = {
            "push_id": str(uuid4()),
            "remote_name": "origin",
            "remote_ref": "refs/heads/main",
            "local_oid": "a" * 40,
            "remote_url_sha256": "d" * 64,
        }
    elif family.startswith("skill"):
        source = "skill"
        tool = "skill.load" if family == "skill_load" else "skill.read_resource"
        executor = tool
        arguments = {
            "name": "build",
            "catalog_sha256": "a" * 64,
            "expected_manifest_sha256": "c" * 64,
        }
        if family == "skill_resource":
            arguments["path"] = "README.md"
    elif family == "mcp":
        source, tool, executor = "mcp", "mcp.calculate", "mcp." + "a" * 32
        arguments = {"value": 1}
    return source, tool, executor, arguments


def success_body(family, plan):
    arguments = plan.invocation.arguments
    if family == "patch":
        return {
            "transaction_id": str(plan.execution.plan_id),
            "files": 1,
            "state": "published",
            "origin": "execution",
            "diff_sha256": "d" * 64,
        }
    if family == "git":
        receipt = {
            "spec_version": "harnessix.git-push-receipt/v1",
            "push_id": arguments["push_id"],
            "remote_name": arguments["remote_name"],
            "remote_ref": arguments["remote_ref"],
            "remote_oid": arguments["local_oid"],
            "remote_url_sha256": arguments["remote_url_sha256"],
            "observed_at": utc_now().isoformat().replace("+00:00", "Z"),
        }
        return {**receipt, "digest": canonical_digest(receipt)}
    if family == "mcp":
        return {
            "spec_version": "harnessix.mcp-tool-call-output/v1",
            "content": [],
            "structured_content": {"value": 2},
            "is_error": False,
        }
    body = {
        "catalog_sha256": arguments["catalog_sha256"],
        "manifest_sha256": arguments["expected_manifest_sha256"],
        "qualified_name": "project/build",
        "content": "public instructions",
        "content_sha256": hashlib.sha256(b"public instructions").hexdigest(),
    }
    if family == "skill_load":
        return {
            "spec_version": "harnessix.skill-content/v1",
            **body,
            "effective_version": "1",
            "resources": [],
        }
    return {
        "spec_version": "harnessix.skill-resource-content/v1",
        **body,
        "path": arguments["path"],
    }


def mutate_body(family, body, case):
    if case == "extra":
        return {**body, "diagnostic": _payload()}
    if case == "missing":
        return {}
    if case == "scalar":
        return _payload()
    if case == "identity":
        field = {
            "patch": "transaction_id",
            "git": "remote_oid",
            "mcp": "is_error",
            "skill_load": "manifest_sha256",
            "skill_resource": "path",
        }[family]
        value = {
            "patch": str(uuid4()),
            "git": "b" * 40,
            "mcp": True,
            "skill_load": "d" * 64,
            "skill_resource": "OTHER.md",
        }[family]
        body = {**body, field: value}
        if family == "git":
            body["digest"] = canonical_digest({k: v for k, v in body.items() if k != "digest"})
    return body


@dataclass
class ReturningBuiltin:
    family: str
    case: str
    calls: int = 0
    reconciliations: int = 0

    async def execute(self, plan, arguments: BaseModel):
        self.calls += 1
        assert isinstance(arguments, McpToolArguments)
        body = mutate_body(self.family, success_body(self.family, plan), self.case)
        return ActionExecutionOutcome(kind="succeeded", output=body, artifact_sha256="b" * 64)

    async def reconcile(self, plan, arguments):
        self.reconciliations += 1
        raise AssertionError("确定成功的公开投影不得对账或重执行")


def builtin_route(root: Path, family, executor):
    root.mkdir()
    (root / "file.txt").write_text("unchanged", encoding="utf-8")
    source, tool, executor_id, arguments = family_data(family)
    read_only = source in {"skill", "mcp"}
    selected = build_trusted_tool_binding(
        source=source,
        source_id="harnessix.product" if source == "builtin" else "test",
        tool=tool,
        executor_id=executor_id,
        tool_version="1",
        tool_fingerprint=canonical_digest(tool),
        input_schema_sha256=canonical_digest(McpToolArguments.model_json_schema()),
        effect_class=EffectClass.READ_ONLY if read_only else EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.LOW if read_only else RiskLevel.HIGH,
        recovery_mode="none" if read_only else "durable_ledger",
    )
    actions, plans, audit = router(root)
    actions.register(
        TrustedActionDefinition(
            selected,
            McpToolArguments,
            lambda *_: ResolvedAction(
                resources=(
                    canonical_action_resource(
                        kind="workspace",
                        access="read" if read_only else "write",
                        identifier={"location": "workspace", "path": "file.txt"},
                    ),
                ),
                workspace_resources=(
                    WorkspaceResourceRequest(
                        path="file.txt", access="read" if read_only else "write"
                    ),
                ),
            ),
            executor,
        )
    )
    invocation = CodingActionInvocation(
        source=source,
        source_id=selected.source_id,
        tool=tool,
        tool_version=selected.tool_version,
        tool_fingerprint=selected.tool_fingerprint,
        arguments=arguments,
        idempotency_key=None if read_only else "builtin-public-output",
    )
    route = actions.plan(invocation, context(root))
    if not read_only:
        actions.decide(
            route.plan.execution.plan_id,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="public-contract-test"),
        )
    return actions, plans, audit, route


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("case", ["valid", "extra", "identity", "missing", "scalar"])
@pytest.mark.parametrize("delivery", ["inline", "owner"])
async def test_builtin_success_requires_schema_and_plan_even_with_a_matching_digest(
    tmp_path, family, case, delivery
):
    root = tmp_path / "workspace"
    executor = ReturningBuiltin(family, case)
    actions, plans, audit, initial = builtin_route(root, family, executor)
    try:
        outcome = await actions.execute(initial.plan.execution.plan_id)
        route = actions.status(initial.plan.execution.plan_id)
        body = outcome.output
        assert route.state == "succeeded"
        assert actions.events(route.plan.execution.plan_id)[-1].output_sha256 == canonical_digest(
            body
        )
        projected = (
            {**body, "artifact": output_reference("b" * 64)} if isinstance(body, dict) else body
        )
        owner = FixedOutput(projected)
        state = SimpleNamespace(
            router=actions, outputs={route.plan.binding.tool: owner} if delivery == "owner" else {}
        )
        thread, turn, call = agent_state(root)
        call = call.model_copy(update={"tool": route.plan.binding.tool})
        before = actions.events(route.plan.execution.plan_id)
        if case == "valid":
            result = await terminal_result(
                state, route, thread, turn, call, outcome, CancelToken(), origin="execution"
            )
            assert result.outcome == "succeeded" and result.output == (
                projected if delivery == "owner" else body
            )
        else:
            with pytest.raises(KernelError) as caught:
                await terminal_result(
                    state, route, thread, turn, call, outcome, CancelToken(), origin="execution"
                )
            assert caught.value.code == "trusted_action_output_mismatch"
            _assert_no_leak(str(caught.value), repr(caught.value))
        assert actions.events(route.plan.execution.plan_id) == before
        assert executor.calls == 1 and executor.reconciliations == 0
        assert owner.calls == (delivery == "owner" and case == "valid")
        assert (root / "file.txt").read_text(encoding="utf-8") == "unchanged"
    finally:
        plans.close()
        audit.close()
