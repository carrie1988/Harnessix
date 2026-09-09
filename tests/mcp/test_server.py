from __future__ import annotations

import os
from pathlib import Path

import pytest
from mcp import Client
from pydantic import BaseModel, ConfigDict

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, RiskLevel
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
    binding = build_trusted_tool_binding(
        source="custom",
        source_id="exporter",
        tool="echo.read",
        tool_version="1",
        tool_fingerprint=canonical_digest("echo.read/v1"),
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
