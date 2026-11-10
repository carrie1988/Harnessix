"""产品MCP装配（ADR 0126）：会话启动时连接已信任的本机Server，工具以“每次审批”接入统一Action入口。

Server自报的只读标注和描述不可信，所有工具都按外部写入、高风险处理；调用发出后的失败为结果未知，
不自动重放，Reconcile只能交给人工。单个Server启动失败不阻塞会话。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.mcp.actions import (
    McpTrustedToolPolicy,
    build_mcp_action_definition,
    mcp_tool_version,
    static_mcp_resource_resolver,
)
from harnessix.mcp.contracts import McpCatalogSnapshot, McpToolSnapshot
from harnessix.mcp.host_target import McpHostStdioTarget
from harnessix.mcp.runtime import McpClientConnection
from harnessix.mcp.store import SQLiteMcpStore
from harnessix.product_config.action_composition import FixedProductActionEnvironment
from harnessix.product_config.mcp_config import ResolvedMcpServer
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, ActionRoutePlan
from harnessix.trusted_actions.router import TrustedActionRouter, canonical_action_resource

_MAX_DESCRIPTION_CHARS = 2048
_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProductMcp:
    """一次启动连上的Server的工具入口，以及没连上的Server和原因。"""

    gateways: tuple[RouterBackedAgentActionGateway, ...]
    failures: tuple[tuple[str, str], ...]


def mcp_host_target(server: ResolvedMcpServer, workspace_root: Path) -> McpHostStdioTarget:
    return McpHostStdioTarget(
        server_id=server.definition.server_id,
        command=server.command,
        arguments=server.arguments,
        working_directory=workspace_root,
        definition_sha256=server.fingerprint,
        environment=server.environment,
        secret_values=server.secret_values,
    )


@asynccontextmanager
async def open_product_mcp(
    router: TrustedActionRouter,
    environment: FixedProductActionEnvironment,
    workspace_root: Path,
    servers: tuple[ResolvedMcpServer, ...],
) -> AsyncIterator[ProductMcp | None]:
    """目录快照只在进程内；会话结束时关闭连接，SDK随之结束Server进程。"""

    if not servers:
        yield None
        return
    async with AsyncExitStack() as stack:
        directory = stack.enter_context(TemporaryDirectory(prefix="harnessix-mcp-"))
        store = stack.enter_context(SQLiteMcpStore(Path(directory) / "mcp.db"))
        gateways: list[RouterBackedAgentActionGateway] = []
        failures: list[tuple[str, str]] = []
        for target in (mcp_host_target(server, workspace_root) for server in servers):
            # 依次连接：SDK的stdio上下文必须在进入它的同一任务里退出，并发连接会让关闭失败。
            result = await _connect(target, store)
            if isinstance(result, str):
                failures.append((target.server_id, result))
                continue
            stack.push_async_callback(result.aclose)
            gateway = _server_gateway(router, environment, result)
            if gateway is not None:
                stack.callback(gateway.close)
                gateways.append(gateway)
        yield ProductMcp(tuple(gateways), tuple(failures))


async def _connect(target: McpHostStdioTarget, store: SQLiteMcpStore) -> McpClientConnection | str:
    try:
        return await McpClientConnection.connect(target, store)
    except KernelError as error:
        _logger.warning("mcp server unavailable: server=%s code=%s", target.server_id, error.code)
        return error.code


def _server_gateway(
    router: TrustedActionRouter,
    environment: FixedProductActionEnvironment,
    connection: McpClientConnection,
) -> RouterBackedAgentActionGateway | None:
    catalog = connection.catalog
    if not catalog.tools:
        return None
    descriptors = []
    for tool in catalog.tools:
        descriptor = _descriptor(connection.server_id, catalog, tool)
        policy = _policy(connection.server_id, tool)
        router.register(build_mcp_action_definition(connection, policy, descriptor=descriptor))
        descriptors.append(descriptor)
    return RouterBackedAgentActionGateway(
        router,
        tuple(descriptors),
        lambda thread, _turn, _call: environment.context(thread.workspace),
        source="mcp",
        source_id=connection.server_id,
    )


def _descriptor(
    server_id: str, catalog: McpCatalogSnapshot, tool: McpToolSnapshot
) -> ToolDescriptor:
    summary = " ".join((tool.description or tool.title or tool.raw_name).split())
    return ToolDescriptor(
        name=tool.model_name,
        version=mcp_tool_version(
            catalog.server.protocol_version, catalog.server.reported_version, tool
        ),
        description=f"[MCP {server_id}] {summary}"[:_MAX_DESCRIPTION_CHARS],
        input_schema=tool.input_schema,
        effect_class=EffectClass.IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        requires_idempotency=False,
        requires_approval=True,
        supports_reconciliation=True,
    )


def _policy(server_id: str, tool: McpToolSnapshot) -> McpTrustedToolPolicy:
    return McpTrustedToolPolicy(
        raw_name=tool.raw_name,
        effect_class=EffectClass.IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        recovery_mode="external_reconcile",
        resolve=static_mcp_resource_resolver(
            canonical_action_resource(
                kind="external", access="write", identifier={"mcp_server": server_id}
            )
        ),
        reconcile=_manual_reconcile,
    )


async def _manual_reconcile(
    _connection: McpClientConnection, plan: ActionRoutePlan, _arguments: object
) -> ActionExecutionOutcome:
    """第三方Server没有可核对的效果记录：结果未知时交给人工，不重放调用。"""

    return ActionExecutionOutcome(
        kind="manual_intervention",
        external_action_id=plan.external_action_id,
        error_code="mcp_effect_unknown",
    )
