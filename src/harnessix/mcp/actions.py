from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from pydantic import BaseModel, JsonValue

from harnessix.agent.errors import KernelError
from harnessix.domain.errors import UncertainEffectError
from harnessix.domain.models import EffectClass, RiskLevel
from harnessix.execution.contracts import canonical_digest
from harnessix.mcp.contracts import McpToolSnapshot
from harnessix.mcp.runtime import McpCallAfterSendError, McpClientConnection
from harnessix.mcp.schema import McpToolArguments, validate_mcp_arguments
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRoutePlan,
    ActionRouteSnapshot,
    CanonicalActionResource,
    RecoveryMode,
    TrustedToolBinding,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ExtensionActionPort,
    ResolvedAction,
    ResourceResolver,
    TrustedActionDefinition,
)

McpReconciler = Callable[
    [McpClientConnection, ActionRoutePlan, McpToolArguments],
    Awaitable[ActionExecutionOutcome],
]


@dataclass(frozen=True, slots=True)
class McpTrustedToolPolicy:
    """完全由宿主持有；不从MCP Annotation或描述推导。"""

    raw_name: str
    effect_class: EffectClass
    risk_level: RiskLevel
    recovery_mode: RecoveryMode
    resolve: ResourceResolver
    reconcile: McpReconciler | None = None

    def __post_init__(self) -> None:
        if self.effect_class is EffectClass.READ_ONLY:
            if self.recovery_mode != "none" or self.reconcile is not None:
                raise KernelError("mcp_trust_policy_invalid", "只读MCP Tool不能声明副作用恢复")
        elif self.recovery_mode != "external_reconcile" or self.reconcile is None:
            raise KernelError("mcp_trust_policy_invalid", "写入MCP Tool必须提供显式外部Reconcile")


class McpTrustedActionExecutor:
    def __init__(
        self,
        connection: McpClientConnection,
        catalog_sha256: str,
        tool: McpToolSnapshot,
        policy: McpTrustedToolPolicy,
    ) -> None:
        self._connection = connection
        self._catalog_sha256 = catalog_sha256
        self._tool = tool
        self._policy = policy

    async def execute(self, plan: ActionRoutePlan, arguments: BaseModel) -> ActionExecutionOutcome:
        parsed = _mcp_arguments(arguments)
        try:
            self._connection.verify_execution_sandbox(
                level=plan.execution.sandbox.level,
                profile_digest=plan.execution.sandbox.profile_digest,
                network=plan.execution.sandbox.network,
            )
            result = await self._connection.call(
                expected_catalog_sha256=self._catalog_sha256,
                expected_tool_sha256=self._tool.tool_sha256,
                raw_name=self._tool.raw_name,
                arguments=parsed.root,
            )
        except KernelError as error:
            return ActionExecutionOutcome(
                kind="failed",
                external_action_id=plan.external_action_id,
                error_code=error.code,
            )
        except McpCallAfterSendError as error:
            if self._policy.effect_class is not EffectClass.READ_ONLY:
                raise UncertainEffectError(error.code) from None
            return ActionExecutionOutcome(
                kind="failed",
                external_action_id=plan.external_action_id,
                error_code=error.code,
            )
        if result.is_error:
            if self._policy.effect_class is not EffectClass.READ_ONLY:
                raise UncertainEffectError("mcp_write_tool_error")
            return ActionExecutionOutcome(
                kind="failed",
                external_action_id=plan.external_action_id,
                error_code="mcp_tool_error",
            )
        return ActionExecutionOutcome(
            kind="succeeded",
            output=cast(JsonValue, result.model_dump(mode="json", warnings="error")),
            external_action_id=plan.external_action_id,
        )

    async def reconcile(
        self, plan: ActionRoutePlan, arguments: BaseModel
    ) -> ActionExecutionOutcome:
        if self._policy.reconcile is None:
            return ActionExecutionOutcome(
                kind="manual_intervention",
                external_action_id=plan.external_action_id,
                error_code="mcp_reconciliation_not_supported",
            )
        return await self._policy.reconcile(
            self._connection,
            plan,
            _mcp_arguments(arguments),
        )


def build_mcp_action_definition(
    connection: McpClientConnection,
    policy: McpTrustedToolPolicy,
) -> TrustedActionDefinition:
    catalog = connection.catalog
    tool = connection.tool(policy.raw_name)
    version = _tool_version(catalog.server.protocol_version, catalog.server.reported_version, tool)
    binding = build_trusted_tool_binding(
        source="mcp",
        source_id=connection.server_id,
        tool=tool.model_name,
        tool_version=version,
        tool_fingerprint=tool.tool_sha256,
        input_schema_sha256=canonical_digest(tool.input_schema),
        effect_class=policy.effect_class,
        risk_level=policy.risk_level,
        recovery_mode=policy.recovery_mode,
        executor_id="mcp."
        + canonical_digest(
            {
                "server": connection.server_id,
                "catalog": catalog.catalog_sha256,
                "tool": tool.tool_sha256,
            }
        )[:32],
    )

    def decode(arguments: dict[str, JsonValue]) -> BaseModel:
        return validate_mcp_arguments(tool.input_schema, arguments)

    return TrustedActionDefinition(
        binding=binding,
        input_model=McpToolArguments,
        resolve=policy.resolve,
        executor=McpTrustedActionExecutor(
            connection,
            catalog.catalog_sha256,
            tool,
            policy,
        ),
        input_schema=tool.input_schema,
        decode_arguments=decode,
    )


class McpActionGateway:
    """模型侧只看到Port已注册且仍绑定当前目录的MCP Tool。"""

    def __init__(self, connection: McpClientConnection, port: ExtensionActionPort) -> None:
        self._connection = connection
        self._port = port

    def tools(self) -> tuple[McpToolSnapshot, ...]:
        bindings = {binding.tool: binding for binding in self._port.bindings()}
        selected = []
        for tool in self._connection.catalog.tools:
            binding = bindings.get(tool.model_name)
            if binding is not None and binding.tool_fingerprint == tool.tool_sha256:
                selected.append(tool.model_copy(deep=True))
        return tuple(selected)

    def plan(
        self,
        *,
        invocation_id: UUID,
        model_name: str,
        arguments: dict[str, JsonValue],
        idempotency_key: str | None = None,
    ) -> ActionRouteSnapshot:
        binding = self._binding(model_name)
        return self._port.plan(
            invocation_id=invocation_id,
            tool=binding.tool,
            tool_version=binding.tool_version,
            tool_fingerprint=binding.tool_fingerprint,
            arguments=arguments,
            idempotency_key=idempotency_key,
        )

    async def execute(self, plan_id: UUID) -> ActionExecutionOutcome:
        return await self._port.execute(plan_id)

    async def reconcile(self, plan_id: UUID) -> ActionExecutionOutcome:
        return await self._port.reconcile(plan_id)

    def _binding(self, model_name: str) -> TrustedToolBinding:
        matches = tuple(binding for binding in self._port.bindings() if binding.tool == model_name)
        if len(matches) != 1:
            raise KernelError("mcp_tool_not_trusted", "MCP Tool未通过可信宿主绑定")
        tool = next(
            (item for item in self._connection.catalog.tools if item.model_name == model_name),
            None,
        )
        if tool is None or tool.tool_sha256 != matches[0].tool_fingerprint:
            raise KernelError("mcp_tool_contract_changed", "MCP Tool目录与可信绑定不一致")
        return matches[0]


def static_mcp_resource_resolver(
    *resources: CanonicalActionResource,
) -> ResourceResolver:
    """仅用于已经由宿主构造的规范资源；不读取不可信参数。"""

    checked = tuple(
        CanonicalActionResource.model_validate_json(resource.model_dump_json())
        for resource in resources
    )

    def resolve(_: BaseModel, __: ActionPlanningContext) -> ResolvedAction:
        return ResolvedAction(resources=checked)

    return resolve


def _mcp_arguments(value: BaseModel) -> McpToolArguments:
    if not isinstance(value, McpToolArguments):
        raise KernelError("mcp_arguments_invalid", "MCP执行参数类型不一致")
    return value


def _tool_version(protocol: str, server_version: str | None, tool: McpToolSnapshot) -> str:
    value = f"{protocol}.{server_version or 'unknown'}.{tool.tool_sha256[:16]}"
    return value[:128]
