from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Protocol, cast

from mcp import Client, StdioServerParameters, stdio_client
from mcp.server import Server
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, InputRequiredResult, Tool
from pydantic import BaseModel, JsonValue, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.execution.contracts import canonical_digest
from harnessix.mcp.contracts import (
    McpCatalogSnapshot,
    McpServerIdentity,
    McpToolCallOutput,
    McpToolSnapshot,
    McpTransportKind,
    mcp_catalog_snapshot_digest,
    mcp_tool_snapshot_digest,
)
from harnessix.mcp.schema import (
    bounded_mcp_output,
    canonical_json_object,
    validate_mcp_input_schema,
    validate_mcp_structured_output,
)
from harnessix.mcp.store import SQLiteMcpStore
from harnessix.sandbox.container import ContainerCommandBuilder, PreparedContainerLaunch
from harnessix.sandbox.contracts import ContainerExecutionSpec, ContainerSandboxProfile
from harnessix.secrets.guard import SecretLeakGuard

MAX_MCP_LIST_PAGES = 1000
MAX_MCP_TOOLS = 2048
MAX_MCP_DEFINITION_BYTES = 256 * 1024
DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS = 30.0
DEFAULT_MCP_CALL_TIMEOUT_SECONDS = 300.0
DEFAULT_MCP_CLOSE_TIMEOUT_SECONDS = 10.0

_MODEL_NAME_INVALID = re.compile(r"[^A-Za-z0-9_.-]")


class McpClientTarget(Protocol):
    server_id: str
    transport: McpTransportKind
    target_sha256: str
    startup_timeout_seconds: float
    call_timeout_seconds: float
    sandbox_profile_digest: str | None
    sandbox_network: str | None

    def build_client(self, stack: AsyncExitStack) -> Client: ...

    async def cleanup(self) -> None: ...

    def redaction_values(self) -> tuple[bytes, ...]: ...


@dataclass(frozen=True, slots=True)
class McpContainerStdioTarget:
    """只接受已由强Container计划生成的stdio启动对象。"""

    server_id: str
    prepared: PreparedContainerLaunch
    execution: ContainerExecutionSpec
    profile: ContainerSandboxProfile
    builder: ContainerCommandBuilder
    startup_timeout_seconds: float = DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS
    call_timeout_seconds: float = DEFAULT_MCP_CALL_TIMEOUT_SECONDS
    transport: McpTransportKind = "container_stdio"

    def __post_init__(self) -> None:
        _validate_server_id(self.server_id)
        if (
            self.prepared.execution_digest != self.execution.digest
            or self.prepared.profile_digest != self.profile.digest
            or self.prepared.container_name != self.builder.container_name(self.execution)
            or self.execution.command.profile_digest != self.profile.digest
            or not self.prepared.argv
            or self.profile.network.policy.mode not in {"none", "limited", "restricted", "full"}
        ):
            raise KernelError("mcp_sandbox_binding_invalid", "MCP Container启动绑定不一致")
        _validate_timeout(self.startup_timeout_seconds, "mcp_startup_timeout_invalid")
        _validate_timeout(self.call_timeout_seconds, "mcp_call_timeout_invalid")

    @property
    def target_sha256(self) -> str:
        return canonical_digest(
            {
                "kind": self.transport,
                "server_id": self.server_id,
                "argv": list(self.prepared.argv),
                "plan": self.prepared.plan_fingerprint,
                "profile": self.profile.digest,
                "execution": self.execution.digest,
            }
        )

    @property
    def sandbox_profile_digest(self) -> str:
        return self.profile.digest

    @property
    def sandbox_network(self) -> str:
        return self.profile.network.policy.mode

    def build_client(self, stack: AsyncExitStack) -> Client:
        stderr = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
        parameters = StdioServerParameters(
            command=self.prepared.argv[0],
            args=list(self.prepared.argv[1:]),
            env=self.prepared.materialize_environment(),
        )
        return Client(
            stdio_client(parameters, errlog=stderr),
            mode="auto",
            read_timeout_seconds=self.call_timeout_seconds,
            cache=None,
        )

    async def cleanup(self) -> None:
        await asyncio.to_thread(self.builder.cleanup_container, self.execution)

    def redaction_values(self) -> tuple[bytes, ...]:
        return self.prepared.redaction_values()


@dataclass(frozen=True, slots=True)
class McpInProcessTarget:
    """受信宿主内嵌或测试Target；不加载第三方Python模块。"""

    server_id: str
    server: Server[Any] | MCPServer
    implementation_digest: str
    startup_timeout_seconds: float = DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS
    call_timeout_seconds: float = DEFAULT_MCP_CALL_TIMEOUT_SECONDS
    transport: McpTransportKind = "in_process"

    def __post_init__(self) -> None:
        _validate_server_id(self.server_id)
        if not re.fullmatch(r"[0-9a-f]{64}", self.implementation_digest):
            raise KernelError("mcp_target_invalid", "MCP内嵌实现摘要无效")
        _validate_timeout(self.startup_timeout_seconds, "mcp_startup_timeout_invalid")
        _validate_timeout(self.call_timeout_seconds, "mcp_call_timeout_invalid")

    @property
    def target_sha256(self) -> str:
        return canonical_digest(
            {
                "kind": self.transport,
                "server_id": self.server_id,
                "implementation": self.implementation_digest,
            }
        )

    @property
    def sandbox_profile_digest(self) -> None:
        return None

    @property
    def sandbox_network(self) -> None:
        return None

    def build_client(self, _: AsyncExitStack) -> Client:
        return Client(
            self.server,
            mode="auto",
            read_timeout_seconds=self.call_timeout_seconds,
            cache=None,
        )

    async def cleanup(self) -> None:
        return None

    def redaction_values(self) -> tuple[bytes, ...]:
        return ()


class McpCallAfterSendError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class McpClientConnection:
    """单服务端Client；调用锁绑定目录刷新、漂移检查和实际调用。"""

    def __init__(
        self,
        *,
        target: McpClientTarget,
        store: SQLiteMcpStore,
        stack: AsyncExitStack,
        client: Client,
        catalog: McpCatalogSnapshot,
    ) -> None:
        self._target = target
        self._store = store
        self._stack = stack
        self._client = client
        self._catalog = catalog
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def connect(
        cls,
        target: McpClientTarget,
        store: SQLiteMcpStore,
    ) -> McpClientConnection:
        store.begin_connect(target.server_id)
        stack = AsyncExitStack()
        try:
            client = target.build_client(stack)
            await asyncio.wait_for(
                stack.enter_async_context(client), timeout=target.startup_timeout_seconds
            )
            catalog = await asyncio.wait_for(
                _capture_catalog(client, target, store.next_generation(target.server_id)),
                timeout=target.startup_timeout_seconds,
            )
            store.connected(catalog)
        except TimeoutError:
            cleanup_failed = await _close_failed_start(stack, target)
            store.failed(
                target.server_id,
                "mcp_process_cleanup_failed" if cleanup_failed else "mcp_startup_timeout",
            )
            if cleanup_failed:
                raise KernelError(
                    "mcp_process_cleanup_failed", "MCP服务端进程无法证明已清理"
                ) from None
            raise KernelError(
                "mcp_startup_timeout", "MCP服务端启动或目录发现超时", retryable=True
            ) from None
        except asyncio.CancelledError:
            cleanup_failed = await _close_failed_start(stack, target)
            store.failed(
                target.server_id,
                "mcp_process_cleanup_failed" if cleanup_failed else "mcp_startup_cancelled",
            )
            raise
        except KernelError as error:
            cleanup_failed = await _close_failed_start(stack, target)
            store.failed(
                target.server_id,
                "mcp_process_cleanup_failed" if cleanup_failed else error.code,
            )
            if cleanup_failed:
                raise KernelError(
                    "mcp_process_cleanup_failed", "MCP服务端进程无法证明已清理"
                ) from None
            raise
        except Exception:
            cleanup_failed = await _close_failed_start(stack, target)
            store.failed(
                target.server_id,
                "mcp_process_cleanup_failed" if cleanup_failed else "mcp_connection_failed",
            )
            if cleanup_failed:
                raise KernelError(
                    "mcp_process_cleanup_failed", "MCP服务端进程无法证明已清理"
                ) from None
            raise KernelError(
                "mcp_connection_failed", "MCP服务端连接失败", retryable=True
            ) from None
        return cls(target=target, store=store, stack=stack, client=client, catalog=catalog)

    @property
    def server_id(self) -> str:
        return self._target.server_id

    @property
    def catalog(self) -> McpCatalogSnapshot:
        return self._catalog.model_copy(deep=True)

    def tool(self, raw_name: str) -> McpToolSnapshot:
        try:
            return next(
                tool.model_copy(deep=True)
                for tool in self._catalog.tools
                if tool.raw_name == raw_name
            )
        except StopIteration:
            raise KernelError("mcp_tool_not_found", "MCP Tool不在捕获目录中") from None

    def verify_execution_sandbox(self, *, level: str, profile_digest: str, network: str) -> None:
        expected_profile = self._target.sandbox_profile_digest
        if expected_profile is None:
            return
        if (
            level != "container_strong"
            or profile_digest != expected_profile
            or network != self._target.sandbox_network
        ):
            raise KernelError("mcp_sandbox_binding_changed", "MCP Action与服务端Sandbox绑定不一致")

    async def call(
        self,
        *,
        expected_catalog_sha256: str,
        expected_tool_sha256: str,
        raw_name: str,
        arguments: dict[str, JsonValue],
    ) -> McpToolCallOutput:
        async with self._lock:
            self._ensure_open()
            await self._verify_fresh_catalog(
                expected_catalog_sha256=expected_catalog_sha256,
                expected_tool_sha256=expected_tool_sha256,
                raw_name=raw_name,
            )
            output_schema = self.tool(raw_name).output_schema
            try:
                result = await asyncio.wait_for(
                    self._client.session.call_tool(
                        raw_name,
                        cast(dict[str, Any], arguments),
                        read_timeout_seconds=self._target.call_timeout_seconds,
                        allow_input_required=True,
                    ),
                    timeout=self._target.call_timeout_seconds,
                )
                if isinstance(result, InputRequiredResult):
                    raise McpCallAfterSendError("mcp_input_required_unsupported")
                if not isinstance(result, CallToolResult):
                    raise McpCallAfterSendError("mcp_result_invalid")
                if not result.is_error and output_schema is not None:
                    validate_mcp_structured_output(output_schema, result.structured_content)
                return _normalize_call_result(result, self._target.redaction_values())
            except asyncio.CancelledError:
                raise
            except McpCallAfterSendError:
                raise
            except KernelError as error:
                raise McpCallAfterSendError(error.code) from None
            except TimeoutError:
                raise McpCallAfterSendError("mcp_tool_timeout") from None
            except RuntimeError:
                if output_schema is not None:
                    raise McpCallAfterSendError("mcp_result_schema_invalid") from None
                self._mark_failed("mcp_connection_failed")
                raise McpCallAfterSendError("mcp_tool_connection_lost") from None
            except Exception:
                self._mark_failed("mcp_connection_failed")
                raise McpCallAfterSendError("mcp_tool_connection_lost") from None

    async def _verify_fresh_catalog(
        self,
        *,
        expected_catalog_sha256: str,
        expected_tool_sha256: str,
        raw_name: str,
    ) -> None:
        if (
            self._catalog.catalog_sha256 != expected_catalog_sha256
            or self.tool(raw_name).tool_sha256 != expected_tool_sha256
        ):
            raise KernelError("mcp_tool_contract_changed", "MCP Tool调用绑定与连接快照不一致")
        try:
            current = await asyncio.wait_for(
                _capture_catalog(
                    self._client,
                    self._target,
                    self._store.next_generation(self.server_id),
                ),
                timeout=self._target.call_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._mark_failed("mcp_catalog_timeout")
            raise KernelError(
                "mcp_catalog_timeout", "MCP调用前目录刷新超时", retryable=True
            ) from None
        except KernelError as error:
            self._mark_failed(error.code)
            raise
        except Exception:
            self._mark_failed("mcp_connection_failed")
            raise KernelError(
                "mcp_connection_failed", "MCP调用前连接失败", retryable=True
            ) from None
        if current.catalog_sha256 != expected_catalog_sha256:
            self._store.schema_changed(current)
            self._catalog = current
            raise KernelError("mcp_tool_schema_changed", "MCP目录已变化，旧计划未执行")
        try:
            current_tool = next(tool for tool in current.tools if tool.raw_name == raw_name)
        except StopIteration:
            self._store.schema_changed(current)
            self._catalog = current
            raise KernelError("mcp_tool_schema_changed", "MCP Tool已移除，旧计划未执行") from None
        if current_tool.tool_sha256 != expected_tool_sha256:
            self._store.schema_changed(current)
            self._catalog = current
            raise KernelError("mcp_tool_schema_changed", "MCP Tool Schema已变化，旧计划未执行")

    def _mark_failed(self, error_code: str) -> None:
        current = self._store.load(self.server_id)
        if current.state in {"connecting", "connected", "schema_changed"}:
            self._store.failed(self.server_id, error_code)

    def _ensure_open(self) -> None:
        if self._closed:
            raise KernelError("mcp_connection_closed", "MCP连接已经关闭")
        current = self._store.load(self.server_id)
        if current.state != "connected":
            raise KernelError("mcp_connection_unavailable", "MCP连接不可执行")

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._finish_close()

    async def _finish_close(self) -> None:
        cleanup_error = False
        try:
            await asyncio.wait_for(self._stack.aclose(), timeout=DEFAULT_MCP_CLOSE_TIMEOUT_SECONDS)
        except Exception:
            cleanup_error = True
        try:
            await asyncio.wait_for(
                self._target.cleanup(), timeout=DEFAULT_MCP_CLOSE_TIMEOUT_SECONDS
            )
        except Exception:
            cleanup_error = True
        current = self._store.load(self.server_id)
        if cleanup_error:
            self._store.failed(self.server_id, "mcp_process_cleanup_failed")
            raise KernelError("mcp_process_cleanup_failed", "MCP服务端进程无法证明已清理")
        if current.state in {"connected", "schema_changed", "failed"}:
            self._store.closed(self.server_id)

    async def __aenter__(self) -> McpClientConnection:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


async def _close_failed_start(stack: AsyncExitStack, target: McpClientTarget) -> bool:
    cleanup_error = False
    try:
        await asyncio.wait_for(stack.aclose(), timeout=DEFAULT_MCP_CLOSE_TIMEOUT_SECONDS)
    except Exception:
        cleanup_error = True
    try:
        await asyncio.wait_for(target.cleanup(), timeout=DEFAULT_MCP_CLOSE_TIMEOUT_SECONDS)
    except Exception:
        cleanup_error = True
    return cleanup_error


async def _capture_catalog(
    client: Client,
    target: McpClientTarget,
    generation: int,
) -> McpCatalogSnapshot:
    tools: list[Tool] = []
    cursors: set[str] = set()
    cursor: str | None = None
    for _ in range(MAX_MCP_LIST_PAGES):
        page = await client.list_tools(cursor=cursor, cache_mode="bypass")
        tools.extend(page.tools)
        if len(tools) > MAX_MCP_TOOLS:
            raise KernelError("mcp_catalog_invalid", "MCP Tool数量超过上限")
        if page.next_cursor is None:
            break
        if page.next_cursor in cursors:
            raise KernelError("mcp_catalog_invalid", "MCP目录返回重复Cursor")
        cursors.add(page.next_cursor)
        cursor = page.next_cursor
    else:
        raise KernelError("mcp_catalog_invalid", "MCP目录分页超过上限")
    raw_names = [tool.name for tool in tools]
    if len(set(raw_names)) != len(raw_names):
        raise KernelError("mcp_catalog_invalid", "MCP目录包含重复Tool名称")
    model_names = _model_tool_names(target.server_id, raw_names)
    snapshots = tuple(
        sorted(
            (_tool_snapshot(tool, model_name=model_names[tool.name]) for tool in tools),
            key=lambda item: item.raw_name,
        )
    )
    capabilities = _model_json(client.server_capabilities)
    capabilities_digest = canonical_digest(capabilities)
    info = client.server_info
    identity = McpServerIdentity(
        server_id=target.server_id,
        transport=target.transport,
        target_sha256=target.target_sha256,
        protocol_version=client.protocol_version,
        reported_name=None if info is None else info.name,
        reported_version=None if info is None else info.version,
        capabilities_sha256=capabilities_digest,
    )
    candidate = McpCatalogSnapshot.model_construct(
        _fields_set=None,
        server=identity,
        generation=generation,
        captured_at=utc_now(),
        tools=snapshots,
        catalog_sha256="0" * 64,
    )
    return McpCatalogSnapshot(
        **candidate.model_dump(exclude={"catalog_sha256"}),
        catalog_sha256=mcp_catalog_snapshot_digest(candidate),
    )


def _tool_snapshot(tool: Tool, *, model_name: str) -> McpToolSnapshot:
    raw = cast(
        dict[str, object],
        tool.model_dump(mode="json", by_alias=True, exclude_none=True, warnings="error"),
    )
    definition_bytes = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(definition_bytes) > MAX_MCP_DEFINITION_BYTES:
        raise KernelError("mcp_catalog_invalid", "MCP Tool定义超过字节上限")
    schema = validate_mcp_input_schema(tool.input_schema)
    output_schema = (
        None if tool.output_schema is None else validate_mcp_input_schema(tool.output_schema)
    )
    annotations = (
        {}
        if tool.annotations is None
        else canonical_json_object(
            tool.annotations.model_dump(
                mode="json", by_alias=True, exclude_none=True, warnings="error"
            ),
            error_code="mcp_catalog_invalid",
        )
    )
    candidate = McpToolSnapshot.model_construct(
        _fields_set=None,
        raw_name=tool.name,
        model_name=model_name,
        title=tool.title,
        description=tool.description,
        input_schema=schema,
        output_schema=output_schema,
        annotations=annotations,
        definition_sha256=hashlib.sha256(definition_bytes).hexdigest(),
        tool_sha256="0" * 64,
    )
    try:
        return McpToolSnapshot(
            **candidate.model_dump(exclude={"tool_sha256"}),
            tool_sha256=mcp_tool_snapshot_digest(candidate),
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError("mcp_catalog_invalid", "MCP Tool定义不符合目录合同") from None


def _model_tool_names(server_id: str, raw_names: list[str]) -> dict[str, str]:
    server = _sanitize_name(server_id)
    candidates = {name: f"mcp__{server}__{_sanitize_name(name)}" for name in raw_names}
    counts: dict[str, int] = {}
    for value in candidates.values():
        counts[value] = counts.get(value, 0) + 1
    result: dict[str, str] = {}
    for raw_name in sorted(raw_names):
        candidate = candidates[raw_name]
        if len(candidate) <= 256 and counts[candidate] == 1:
            result[raw_name] = candidate
            continue
        suffix = hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:12]
        result[raw_name] = candidate[:241] + "__" + suffix
    if len(set(result.values())) != len(result):
        raise KernelError("mcp_catalog_invalid", "MCP Tool模型名称无法唯一化")
    return result


def _sanitize_name(value: str) -> str:
    normalized = _MODEL_NAME_INVALID.sub("_", value)
    if not normalized or not normalized[0].isalpha():
        normalized = "x_" + normalized
    return normalized


def _normalize_call_result(
    result: CallToolResult, redaction_values: tuple[bytes, ...]
) -> McpToolCallOutput:
    raw_content = cast(
        list[object],
        [
            item.model_dump(mode="json", by_alias=True, exclude_none=True, warnings="error")
            for item in result.content
        ],
    )
    payload = {
        "content": raw_content,
        "structured_content": result.structured_content,
        "is_error": result.is_error,
    }
    checked = bounded_mcp_output(payload)
    if not isinstance(checked, dict):
        raise KernelError("mcp_result_invalid", "MCP Tool结果结构无效")
    redacted = SecretLeakGuard(redaction_values).redact_json(checked)
    try:
        return McpToolCallOutput.model_validate_json(
            json.dumps(redacted, ensure_ascii=False, allow_nan=False)
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError("mcp_result_invalid", "MCP Tool结果不符合输出合同") from None


def _model_json(value: BaseModel | None) -> JsonValue:
    if value is None:
        return {}
    return bounded_mcp_output(
        value.model_dump(mode="json", by_alias=True, exclude_none=True, warnings="error")
    )


def _validate_server_id(value: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,255}", value):
        raise KernelError("mcp_server_id_invalid", "MCP服务端身份无效")


def _validate_timeout(value: float, code: str) -> None:
    if type(value) not in {int, float} or not 0.1 <= value <= 3600:
        raise KernelError(code, "MCP超时必须位于0.1至3600秒")
