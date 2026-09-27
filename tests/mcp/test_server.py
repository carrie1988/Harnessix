from __future__ import annotations

import os
from pathlib import Path

import pytest
from mcp import Client
from pydantic import BaseModel, ConfigDict

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.execution.contracts import SandboxBindingV2, canonical_digest
from harnessix.execution.planner import build_capability_evidence_v2
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.mcp import HarnessixMcpServer, McpExportedTool
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str


class EchoExecutor:
    async def execute(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        parsed = EchoInput.model_validate(arguments)
        return ActionExecutionOutcome(kind="succeeded", output={"echo": parsed.text})

    async def reconcile(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        return ActionExecutionOutcome(
            kind="manual_intervention",
            error_code="reconciliation_not_supported",
        )


def action_context(root: Path) -> ActionPlanningContext:
    capabilities = build_capability_evidence_v2(
        platform="windows" if os.name == "nt" else "posix",
        provider="native",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("none",),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
        provider_evidence_digest=canonical_digest("test-native-provider"),
    )
    return ActionPlanningContext(
        workspace_root=root,
        sandbox=SandboxBindingV2(
            level="host_guarded",
            backend="host",
            backend_version="1",
            network="none",
            capability_digest=capabilities.evidence_digest,
            profile_digest=canonical_digest("test-profile"),
        ),
        capabilities=capabilities,
    )


def exported_server(
    tmp_path: Path,
    *,
    effect_class: EffectClass = EffectClass.READ_ONLY,
    risk_level: RiskLevel = RiskLevel.LOW,
    recovery_mode: str = "none",
) -> HarnessixMcpServer:
    root = tmp_path / "workspace"
    root.mkdir()
    descriptor = ToolDescriptor(
        name="echo.read",
        version="1",
        description="返回输入文本",
        input_schema=EchoInput.model_json_schema(),
        effect_class=effect_class,
        risk_level=risk_level,
        requires_idempotency=False,
        requires_approval=effect_class is not EffectClass.READ_ONLY,
        supports_reconciliation=recovery_mode != "none",
        public_output_schema={
            "type": "object",
            "properties": {"echo": {"type": "string", "maxLength": 4096}},
            "required": ["echo"],
            "additionalProperties": False,
        },
    )
    binding = build_trusted_tool_binding(
        source="custom",
        source_id="exporter",
        tool="echo.read",
        tool_version="1",
        tool_fingerprint=canonical_digest(descriptor.model_dump(mode="json")),
        input_schema_sha256=canonical_digest(EchoInput.model_json_schema()),
        effect_class=effect_class,
        risk_level=risk_level,
        recovery_mode=recovery_mode,
        executor_id="test.echo",
    )
    router = TrustedActionRouter(
        plans=SQLiteExecutionPlanStore(tmp_path / "plans.db"),
        audit=SQLiteActionAuditStore(tmp_path / "actions.db"),
        workspace_root=lambda _: root,
    )
    router.register(
        TrustedActionDefinition(
            binding=binding,
            input_model=EchoInput,
            resolve=lambda *_: ResolvedAction(resources=()),
            executor=EchoExecutor(),
        )
    )
    port = router.extension_port(
        source="custom",
        source_id="exporter",
        context=lambda: action_context(root),
    )
    return HarnessixMcpServer(
        port=port,
        exports=(
            McpExportedTool(
                public_name="echo",
                binding_name="echo.read",
                description="返回输入文本",
                input_schema=EchoInput.model_json_schema(),
                descriptor=descriptor,
            ),
        ),
    )


async def test_optional_server_lists_and_executes_only_exported_read_tool(
    tmp_path: Path,
) -> None:
    server = exported_server(tmp_path)

    async with Client(server.low_level_server, cache=None) as client:
        listed = await client.list_tools(cache_mode="bypass")
        assert [item.name for item in listed.tools] == ["echo"]

        success = await client.call_tool("echo", {"text": "Harnessix"})
        assert success.is_error is False
        assert success.structured_content == {"echo": "Harnessix"}

        invalid = await client.call_tool("echo", {"unexpected": True})
        assert invalid.is_error is True
        assert "tool_invalid_arguments" in invalid.content[0].text

        missing = await client.call_tool("missing", {})
        assert missing.is_error is True
        assert "mcp_export_not_found" in missing.content[0].text


def test_optional_server_rejects_write_action_export(tmp_path: Path) -> None:
    with pytest.raises(KernelError) as caught:
        exported_server(
            tmp_path,
            effect_class=EffectClass.IDEMPOTENT_WRITE,
            risk_level=RiskLevel.HIGH,
            recovery_mode="external_reconcile",
        )
    assert caught.value.code == "mcp_export_policy_invalid"


@pytest.mark.parametrize("case", ["undeclared", "extra", "valid"])
async def test_mcp_export_has_its_own_bound_public_output_contract(tmp_path, case):
    from harnessix.mcp.server import McpExportedTool
    from tests.trusted_actions.test_public_error_leakage import _assert_no_leak, _payload

    server = exported_server(tmp_path)
    original = server._exports[0]
    if case == "undeclared":
        # 旧导出没有公开描述；不能因只读、Schema匹配或执行成功而泄漏正文。
        exported = McpExportedTool(
            original.public_name, original.binding_name, original.description, original.input_schema
        )
        server = HarnessixMcpServer(port=server._port, exports=(exported,))
    actions = server._port._ExtensionActionPort__router
    definition = next(iter(actions._definitions.values()))

    class BodyExecutor:
        calls = 0

        async def execute(self, plan, arguments):
            self.calls += 1
            body = {"echo": "public"}
            if case != "valid":
                body["diagnostic"] = _payload()
            return ActionExecutionOutcome(kind="succeeded", output=body)

    executor = BodyExecutor()
    import dataclasses

    actions._definitions[next(iter(actions._definitions))] = dataclasses.replace(
        definition, executor=executor
    )
    try:
        async with Client(server.low_level_server, cache=None) as client:
            result = await client.call_tool("echo", {"text": "hello"})
            assert result.is_error is (case != "valid")
            if case == "valid":
                assert result.structured_content == {"echo": "public"}
            _assert_no_leak(result.model_dump_json())
        assert executor.calls == 1
    finally:
        actions._audit.close()
        actions._plans.close()


def test_mcp_export_rejects_descriptor_not_bound_to_registered_fingerprint(tmp_path):
    import dataclasses

    server = exported_server(tmp_path)
    original = server._exports[0]
    forged = original.descriptor.model_copy(update={"description": "changed"})
    try:
        with pytest.raises(KernelError) as caught:
            HarnessixMcpServer(
                port=server._port, exports=(dataclasses.replace(original, descriptor=forged),)
            )
        assert caught.value.code == "mcp_export_contract_invalid"
    finally:
        actions = server._port._ExtensionActionPort__router
        actions._audit.close()
        actions._plans.close()


@pytest.mark.parametrize("case", ["safe", "leak", "closed"])
async def test_explicit_secret_scope_at_independent_mcp_client(tmp_path, case):
    from harnessix.secrets.publication import SecretPublicationScope
    from tests.trusted_actions.test_secret_publication import BINDING, CANARY, source

    scope = SecretPublicationScope((BINDING,), source())
    prior = exported_server(tmp_path)
    server = HarnessixMcpServer(port=prior._port, exports=prior._exports, secret_scope=scope)
    if case == "closed":
        scope.close()
    try:
        async with Client(server.low_level_server, cache=None) as client:
            result = await client.call_tool(
                "echo", {"text": "completed" if case == "safe" else CANARY}
            )
        assert bool(result.is_error) == (case != "safe")
        assert CANARY not in result.model_dump_json()
        if case == "safe":
            assert result.structured_content == {"echo": "completed"}
        else:
            assert (
                "trusted_action_secret_leak"
                if case == "leak"
                else "trusted_action_secret_unavailable"
            ) in result.content[0].text
    finally:
        scope.close()
