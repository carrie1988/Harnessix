"""固定Product Process Profile的公共Tool、可信执行器和输出发布器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread, ToolCallContent, Turn
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ContractModel, EffectClass, RiskLevel, ToolDescriptor
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.planner import bind_environment
from harnessix.processes.supervision_contracts import ProcessLease
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.trusted_output import (
    TrustedProcessOutputDocument,
    build_trusted_process_output,
)
from harnessix.sandbox.contracts import ContainerExecutionSpec
from harnessix.sandbox.planner import build_container_command, build_container_execution
from harnessix.secrets.provider import SecretProvider, resolve_secret_environment
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRoutePlan,
    ActionRouteSnapshot,
    CanonicalActionResource,
    TrustedToolBinding,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
    canonical_action_resource,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest

from .action_contracts import ProductProcessProfile
from .process_profile import VerifiedProductProcessProfile, process_tool_name

PRODUCT_PROCESS_VERSION = "harnessix.product-process/v1"
_ACTION_OUTPUT_NAMESPACE = UUID("ee9ec0c4-2584-41d2-a82d-7679d687b20c")
_SHELL_OPERATOR = re.compile(r"[;&|`$<>\r\n\x00]")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
WorkspaceRootResolver = Callable[[str], Path]


class RunProfileInput(ContractModel):
    """模型只能选择固定Profile和有界测试选择器，不能提供程序或环境。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    selectors: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("selectors")
    @classmethod
    def safe_selectors(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        total = 0
        for value in values:
            if type(value) is not str:
                raise ValueError("Process选择器类型无效")
            try:
                size = len(value.encode("utf-8"))
            except UnicodeError:
                raise ValueError("Process选择器编码无效") from None
            total += size
            path = value.split("::", 1)[0]
            segments = re.split(r"[/\\]", path)
            if (
                not value
                or value != value.strip()
                or size > 512
                or total > 8192
                or value.startswith("-")
                or value.startswith(("/", "\\"))
                or _WINDOWS_DRIVE.match(value)
                or _SHELL_OPERATOR.search(value)
                or ".." in segments
            ):
                raise ValueError("Process选择器超出安全边界")
        return values


def run_profile_schema(profile_id: str) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {
            "profile": {"type": "string", "const": profile_id},
            "selectors": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 512},
                "maxItems": 32,
                "default": [],
            },
        },
        "required": ["profile"],
        "additionalProperties": False,
    }


def decode_run_profile(
    profile_id: str,
    selector_policy: Literal["none", "bounded_test_selector"],
    arguments: dict[str, JsonValue],
) -> RunProfileInput:
    try:
        checked = RunProfileInput.model_validate_json(
            json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        )
    except (ValidationError, ValueError, TypeError):
        raise ValueError("Process Profile参数无效") from None
    if checked.profile != profile_id or (selector_policy == "none" and checked.selectors):
        raise ValueError("Process Profile或选择器策略不匹配")
    return checked


def process_profile_descriptor(profile: ProductProcessProfile) -> ToolDescriptor:
    return ToolDescriptor(
        name=process_tool_name(profile.profile_id),
        version=f"{PRODUCT_PROCESS_VERSION}:{profile.profile_sha256[:24]}",
        description=(
            f"在固定无网络只读容器中运行“{profile.description}”（Profile {profile.profile_id} "
            f"{profile.version}）；只能提供受限测试选择器，程序、镜像、资源、环境和Secret由宿主冻结。"
        ),
        input_schema=run_profile_schema(profile.profile_id),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        requires_idempotency=True,
        requires_approval=True,
        supports_reconciliation=True,
        supports_parallel_calls=False,
    )


def product_process_binding(profile: ProductProcessProfile) -> TrustedToolBinding:
    """从固定Profile构造稳定Binding，供运行时和无状态Doctor共同复用。"""

    descriptor = process_profile_descriptor(profile)
    return build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix.product",
        tool=descriptor.name,
        tool_version=descriptor.version,
        tool_fingerprint=tool_fingerprint(descriptor),
        input_schema_sha256=canonical_digest(descriptor.input_schema),
        effect_class=descriptor.effect_class,
        risk_level=descriptor.risk_level,
        recovery_mode="durable_ledger",
        executor_id=f"product.process-profile.{profile.profile_id}",
    )


def _arguments(value: BaseModel, owner: VerifiedProductProcessProfile) -> RunProfileInput:
    try:
        dumped = cast(dict[str, JsonValue], value.model_dump(mode="json"))
        return decode_run_profile(
            owner.profile.profile_id,
            owner.profile.selector_policy,
            dumped,
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError(
            "process_profile_arguments_invalid", "Process Profile参数类型不一致"
        ) from None


def resolve_run_profile(
    owner: VerifiedProductProcessProfile,
    arguments: RunProfileInput,
    context: ActionPlanningContext,
) -> ResolvedAction:
    """绑定固定Profile、只读Workspace、Owner能力和Secret版本资源。"""

    if (
        context.cwd != "."
        or context.capabilities != owner.capabilities
        or context.sandbox != owner.sandbox
        or dict(context.environment) != dict(owner.environment)
        or context.secrets != owner.secret_bindings
        or context.external_roots is not None
    ):
        raise KernelError("process_profile_context_mismatch", "Process Profile规划上下文不匹配")
    resources: list[CanonicalActionResource] = [
        canonical_action_resource(
            kind="process",
            access="execute",
            identifier={"profile": owner.profile.profile_id},
            attributes={
                "profile_sha256": owner.profile.profile_sha256,
                "image_attestation_sha256": owner.image_attestation_sha256,
                "owner_capability_sha256": owner.runtime.capability.digest,
                "selectors": arguments.selectors,
            },
        ),
        canonical_action_resource(
            kind="workspace",
            access="read",
            identifier={"location": "workspace", "path": "."},
            attributes={"mount": "read_only"},
        ),
    ]
    resources.extend(
        canonical_action_resource(
            kind="secret",
            access="use",
            identifier={"name": binding.name, "target": binding.target},
            attributes={"version": binding.version},
        )
        for binding in owner.secret_bindings
    )
    return ResolvedAction(
        resources=tuple(resources),
        workspace_resources=(WorkspaceResourceRequest(path=".", access="read"),),
    )


def _process_execution(
    owner: VerifiedProductProcessProfile,
    route: ActionRoutePlan,
    arguments: RunProfileInput,
) -> ContainerExecutionSpec:
    """由固定Profile和批准参数确定性派生Container执行合同。"""

    profile = owner.profile
    argv = (profile.program, *profile.arguments, *arguments.selectors)
    command = build_container_command(argv, profile_digest=owner.sandbox_profile.digest)
    process = build_process_spec(
        invocation="argv",
        argv=argv,
        terminal="pipe",
        stdin="closed",
        lifecycle="foreground",
        timeout_seconds=profile.timeout_seconds,
        output_bytes=profile.max_output_bytes,
        input_bytes=0,
        process_id=route.execution.plan_id,
    )
    return build_container_execution(
        command,
        process,
        owner_capability_digest=owner.runtime.capability.digest,
    )


def _validate_process_route(
    owner: VerifiedProductProcessProfile,
    workspace_root: WorkspaceRootResolver,
    route: ActionRoutePlan,
    arguments: RunProfileInput,
) -> None:
    """重新解析公共参数并核对Action Route所有安全绑定。"""

    resolved = resolve_run_profile(
        owner,
        arguments,
        ActionPlanningContext(
            workspace_root=workspace_root(route.execution.workspace.workspace_id),
            sandbox=owner.sandbox,
            capabilities=owner.capabilities,
            environment=owner.environment,
            secrets=owner.secret_bindings,
        ),
    )
    expected = tuple(
        sorted(
            resolved.resources,
            key=lambda item: (
                item.kind,
                item.access,
                item.identifier_sha256,
                item.attributes_sha256,
            ),
        )
    )
    descriptor = process_profile_descriptor(owner.profile)
    if (
        route.binding.tool != descriptor.name
        or route.binding.executor_id != f"product.process-profile.{owner.profile.profile_id}"
        or route.invocation.arguments != arguments.model_dump(mode="json")
        or route.resources != expected
        or route.execution.capabilities != owner.capabilities
        or route.execution.sandbox != owner.sandbox
        or route.execution.environment
        != bind_environment(owner.environment, platform=route.execution.workspace.platform)
        or route.execution.secrets != owner.secret_bindings
    ):
        raise KernelError("process_profile_plan_mismatch", "Process Profile与Action Route不匹配")


async def _process_output_document(
    owner: VerifiedProductProcessProfile,
    plan_id: UUID,
) -> TrustedProcessOutputDocument:
    """从Owner Ledger及持久输出重建经过摘要校验的终态文档。"""

    lease = owner.runtime.status(plan_id)
    if lease.state not in {"exited", "failed", "unknown"}:
        raise KernelError("process_not_terminal", "Process尚未形成终态输出")
    stdout, stderr = await asyncio.gather(
        owner.runtime.output(plan_id, "stdout"),
        owner.runtime.output(plan_id, "stderr"),
    )
    try:
        return build_trusted_process_output(owner.profile.profile_id, lease, stdout, stderr)
    except ValueError:
        raise KernelError("process_output_corrupt", "Process终态输出与Lease不一致") from None


async def _process_lease_outcome(
    owner: VerifiedProductProcessProfile,
    lease: ProcessLease,
    *,
    origin: Literal["execution", "recovery"],
) -> ActionExecutionOutcome:
    """把Owner终态转换为Router可持久化的确定结果或诚实不确定态。"""

    try:
        document = await _process_output_document(owner, lease.process_id)
    except KernelError:
        return ActionExecutionOutcome(
            kind="unknown" if origin == "execution" else "manual_intervention",
            error_code="process_output_unavailable",
        )
    summary = document.summary
    if lease.state == "unknown":
        kind: Literal["succeeded", "failed", "unknown", "manual_intervention"] = (
            "unknown" if origin == "execution" else "manual_intervention"
        )
        error_code = "process_state_unknown"
    elif lease.state == "failed":
        kind, error_code = "failed", "process_launch_failed"
    elif lease.stop_reason == "exited" and lease.returncode == 0:
        kind, error_code = "succeeded", None
    elif lease.stop_reason == "exited":
        kind, error_code = "failed", "process_nonzero_exit"
    else:
        kind, error_code = (
            "failed",
            {
                "timeout": "process_timeout",
                "cancelled": "process_cancelled",
                "output_limit": "process_output_limit",
                "input_limit": "process_input_limit",
                "io_error": "process_io_error",
                "closed": "process_closed",
                "cleanup_failed": "process_cleanup_failed",
                "host_lost": "process_host_lost",
                "unknown": "process_state_unknown",
                "launch_failed": "process_launch_failed",
            }.get(lease.stop_reason or "unknown", "process_failed"),
        )
    body = document.to_jsonl()
    return ActionExecutionOutcome(
        kind=kind,
        output=summary.public_output(),
        artifact_sha256=hashlib.sha256(body).hexdigest(),
        error_code=error_code,
    )


async def _process_execution_failure(
    owner: VerifiedProductProcessProfile,
    plan_id: UUID,
    error: KernelError,
) -> ActionExecutionOutcome:
    """根据可证明的Lease存在性区分前置失败与副作用不确定。"""

    try:
        lease = owner.runtime.status(plan_id)
    except KernelError as missing:
        if missing.code != "process_lease_not_found":
            return ActionExecutionOutcome(kind="unknown", error_code="process_state_unavailable")
        deterministic = {
            "sandbox_binding_invalid",
            "sandbox_binding_changed",
            "sandbox_capability_mismatch",
            "process_capability_mismatch",
            "approval_required",
            "execution_plan_stale",
            "secret_binding_mismatch",
            "secret_unavailable",
            "secret_version_changed",
        }
        return ActionExecutionOutcome(
            kind="failed" if error.code in deterministic else "unknown",
            error_code=(
                "process_preflight_failed"
                if error.code in deterministic
                else "process_effect_unknown"
            ),
        )
    return await _process_lease_outcome(owner, lease, origin="execution")


class ProductProcessActionExecutor:
    """从批准的公共参数确定性派生Container执行合同并只经Process Owner运行。"""

    def __init__(
        self,
        owner: VerifiedProductProcessProfile,
        router: TrustedActionRouter,
        workspace_root: WorkspaceRootResolver,
        secrets: SecretProvider,
    ) -> None:
        self.owner = owner
        self._router = router
        self._workspace_root = workspace_root
        self._secrets = secrets

    async def execute(
        self,
        route: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        checked = _arguments(arguments, self.owner)
        _validate_process_route(self.owner, self._workspace_root, route, checked)
        execution = _process_execution(self.owner, route, checked)
        checkpoint = self._router.approval(route.execution.plan_id)
        workspace = self._workspace_root(route.execution.workspace.workspace_id)
        try:
            with resolve_secret_environment(
                route.execution.secrets,
                self._secrets,
                platform=route.execution.workspace.platform,
            ) as secrets:
                lease = await self.owner.runtime.run(
                    route.execution,
                    checkpoint,
                    self.owner.sandbox_profile,
                    execution,
                    workspace=workspace,
                    environment=self.owner.environment,
                    intent_arguments=route.invocation.arguments,
                    secrets=secrets,
                )
        except asyncio.CancelledError:
            raise
        except KernelError as error:
            return await _process_execution_failure(self.owner, route.execution.plan_id, error)
        return await _process_lease_outcome(self.owner, lease, origin="execution")

    async def reconcile(
        self,
        route: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        checked = _arguments(arguments, self.owner)
        _validate_process_route(self.owner, self._workspace_root, route, checked)
        execution = _process_execution(self.owner, route, checked)
        try:
            lease = await self.owner.runtime.reconcile(execution)
        except KernelError as error:
            if error.code == "process_lease_not_found":
                return ActionExecutionOutcome(kind="failed", error_code="process_not_started")
            return ActionExecutionOutcome(
                kind="manual_intervention",
                error_code="process_reconciliation_failed",
            )
        return await _process_lease_outcome(self.owner, lease, origin="recovery")

    async def output_document(self, plan_id: UUID) -> TrustedProcessOutputDocument:
        return await _process_output_document(self.owner, plan_id)


class ProductProcessOutputProvider:
    """把Owner重建正文绑定到Router审计摘要并查询优先发布Artifact。"""

    def __init__(
        self,
        executor: ProductProcessActionExecutor,
        artifacts: SQLiteArtifactStore,
        *,
        workspace_scope: str,
    ) -> None:
        self._executor = executor
        self._artifacts = artifacts
        self._workspace_scope = workspace_scope

    async def output(
        self,
        route: ActionRouteSnapshot,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        *,
        expected_output_sha256: str,
        expected_artifact_sha256: str,
        cancel: CancelToken,
    ) -> JsonValue:
        cancel.checkpoint()
        document = await self._executor.output_document(route.plan.execution.plan_id)
        body = document.to_jsonl()
        public = document.summary.public_output()
        if (
            canonical_digest(public) != expected_output_sha256
            or hashlib.sha256(body).hexdigest() != expected_artifact_sha256
        ):
            raise KernelError("trusted_action_output_mismatch", "Process输出与Router审计摘要不匹配")
        ref = await self._artifacts.publish_action_output(
            thread.thread_id,
            turn.turn_id,
            call,
            body,
            artifact_id=uuid5(_ACTION_OUTPUT_NAMESPACE, str(route.plan.execution.plan_id)),
            workspace_scope=self._workspace_scope,
            expected_sequence=thread.sequence,
        )
        return cast(JsonValue, {**public, "artifact": ref.model_dump(mode="json")})


def build_product_process_definition(
    owner: VerifiedProductProcessProfile,
    router: TrustedActionRouter,
    workspace_root: WorkspaceRootResolver,
    secrets: SecretProvider,
) -> tuple[TrustedActionDefinition, ProductProcessActionExecutor, ToolDescriptor]:
    """从同一动态Schema构造Descriptor、Router Binding与执行器。"""

    descriptor = process_profile_descriptor(owner.profile)
    binding = product_process_binding(owner.profile)
    executor = ProductProcessActionExecutor(owner, router, workspace_root, secrets)

    def decode(arguments: dict[str, JsonValue]) -> BaseModel:
        return decode_run_profile(
            owner.profile.profile_id,
            owner.profile.selector_policy,
            arguments,
        )

    def resolve(arguments: BaseModel, context: ActionPlanningContext) -> ResolvedAction:
        return resolve_run_profile(owner, _arguments(arguments, owner), context)

    definition = TrustedActionDefinition(
        binding=binding,
        input_model=RunProfileInput,
        resolve=resolve,
        executor=executor,
        input_schema=descriptor.input_schema,
        decode_arguments=decode,
    )
    return definition, executor, descriptor
