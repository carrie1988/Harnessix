"""固定Product Process Profile的公共Tool、可信执行器和输出发布器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping
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
    recorded_process_public_output,
    trusted_process_public_output,
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
from harnessix.workspace.contracts import ResourceAccess, WorkspaceResourceRequest

from .action_contracts import ProductProcessProfile, SelectorPolicy
from .process_command import (
    PRODUCT_COMMAND_VERSION,
    OptionalProfileId,
    RunCommandInput,
    decode_run_command,
    run_command_schema,
)
from .process_profile import (
    VerifiedProductProcessProfile,
    process_executor_id,
    process_tool_name,
)
from .task_dependencies import TaskDependencyEnvironments

PRODUCT_PROCESS_VERSION = "harnessix.product-process/v2"
_ACTION_OUTPUT_NAMESPACE = UUID("ee9ec0c4-2584-41d2-a82d-7679d687b20c")
_SHELL_OPERATOR = re.compile(r"[;&|`$<>\r\n\x00]")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
WorkspaceRootResolver = Callable[[str], Path]


class RunProfileInput(ContractModel):
    """模型只能提供有界测试选择器，不能提供程序或环境；Profile由工具名绑定，传入时必须等于固定值。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile: OptionalProfileId = None
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


def run_profile_schema(
    profile_id: str,
    selector_policy: Literal["none", "bounded_test_selector"] = "bounded_test_selector",
) -> dict[str, JsonValue]:
    """广告可省略的固定Profile及实际选择器策略；不自动补全模型参数。"""
    return {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "const": profile_id,
                "description": f'可省略：工具名已确定Profile；传入时必须等于固定值"{profile_id}"。',
            },
            "selectors": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 512},
                "maxItems": 0 if selector_policy == "none" else 32,
                "default": [],
                "description": (
                    "可省略或传入空数组；此Profile不接受测试选择器。"
                    if selector_policy == "none"
                    else "可省略或传入空数组以执行固定检查；非空时仅接受有界相对测试选择器。"
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    }


def decode_run_profile(
    profile_id: str,
    selector_policy: SelectorPolicy,
    arguments: dict[str, JsonValue],
) -> RunProfileInput:
    if selector_policy not in {"none", "bounded_test_selector"}:
        raise ValueError("Process Profile选择器策略无效")
    try:
        checked = RunProfileInput.model_validate_json(
            json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        )
    except (ValidationError, ValueError, TypeError):
        raise ValueError("Process Profile参数无效") from None
    if checked.profile not in (None, profile_id) or (
        selector_policy == "none" and checked.selectors
    ):
        raise ValueError("Process Profile或选择器策略不匹配")
    return checked


def process_profile_descriptor(profile: ProductProcessProfile) -> ToolDescriptor:
    if profile.selector_policy == "shell_command":
        return ToolDescriptor(
            name=process_tool_name(profile.profile_id, profile.selector_policy),
            version=f"{PRODUCT_COMMAND_VERSION}:{profile.profile_sha256[:24]}",
            description=(
                f"在固定无网络只读容器中执行Shell命令“{profile.description}”（{profile.profile_id} "
                f'{profile.version}）；必填"cmd"，"profile"可省略（工具名已确定，'
                f'传入时须为"{profile.profile_id}"）。cmd必须非空白，允许管道、重定向和多行，'
                "不含NUL且最多16384 UTF-8字节。"
                "宿主冻结Shell、镜像、工作目录、资源、环境和Secret。"
                + (
                    "任务依赖目录/harnessix/deps可读写并跨命令保留；源码仍只读。"
                    if profile.persistent_dependencies
                    else ""
                )
                + (
                    f"工作区源码挂载在容器内{profile.workspace_path}，命令从该目录开始。"
                    if profile.workspace_path is not None
                    else ""
                )
                + (
                    "每次命令使用工作区的一次性可写快照：可写入构建产物和临时文件，"
                    "命令结束后全部丢弃，修改源码仍须用补丁工具。"
                    if profile.workspace_mode == "snapshot"
                    else ""
                )
                + (
                    "预设环境变量：" + "、".join(name for name, _ in profile.environment) + "。"
                    if profile.environment
                    else ""
                )
                + "失败先看diagnostic_preview；完整时不重读日志Artifact。"
            ),
            input_schema=run_command_schema(profile.profile_id),
            effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
            risk_level=RiskLevel.HIGH,
            requires_idempotency=True,
            requires_approval=True,
            supports_reconciliation=True,
            supports_parallel_calls=False,
        )
    return ToolDescriptor(
        name=process_tool_name(profile.profile_id),
        version=f"{PRODUCT_PROCESS_VERSION}:{profile.profile_sha256[:24]}",
        description=(
            f"在固定无网络只读容器中运行“{profile.description}”（{profile.profile_id} "
            f'{profile.version}）；"profile"可省略（工具名已确定，传入时须为'
            f'"{profile.profile_id}"）；selectors可省略或为空数组。'
            + (
                "此Profile不接受非空选择器。"
                if profile.selector_policy == "none"
                else "非空选择器仅允许受限相对测试目标。"
            )
            + "宿主冻结程序、镜像、资源、环境和Secret。"
            "失败先看diagnostic_preview；完整时不重读日志Artifact。"
        ),
        input_schema=run_profile_schema(profile.profile_id, profile.selector_policy),
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
        executor_id=process_executor_id(profile),
    )


def _decode_arguments(
    profile: ProductProcessProfile,
    arguments: dict[str, JsonValue],
) -> RunProfileInput | RunCommandInput:
    if profile.selector_policy == "shell_command":
        return decode_run_command(profile.profile_id, arguments)
    return decode_run_profile(profile.profile_id, profile.selector_policy, arguments)


def _arguments(
    value: BaseModel,
    owner: VerifiedProductProcessProfile,
) -> RunProfileInput | RunCommandInput:
    try:
        dumped = cast(dict[str, JsonValue], value.model_dump(mode="json"))
        return _decode_arguments(owner.profile, dumped)
    except (ValidationError, ValueError, TypeError):
        raise KernelError(
            "process_profile_arguments_invalid", "Process Profile参数类型不一致"
        ) from None


def resolve_run_profile(
    owner: VerifiedProductProcessProfile,
    arguments: RunProfileInput | RunCommandInput,
    context: ActionPlanningContext,
    *,
    dependency_environments: TaskDependencyEnvironments | None = None,
    allow_quarantined: bool = False,
) -> ResolvedAction:
    """绑定固定Profile、只读Workspace、Owner能力和Secret版本资源。"""

    arguments = _arguments(arguments, owner)
    if (
        context.cwd != "."
        or context.capabilities != owner.capabilities
        or context.sandbox != owner.sandbox
        or dict(context.environment) != dict(owner.environment)
        or context.secrets != owner.secret_bindings
    ):
        raise KernelError("process_profile_context_mismatch", "Process Profile规划上下文不匹配")
    if owner.profile.persistent_dependencies:
        if dependency_environments is None:
            raise KernelError("task_dependencies_unavailable", "任务依赖目录尚未绑定宿主Registry")
        dependency_environments.validate(
            cast(
                Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]],
                context.external_roots,
            ),
            owner.profile,
            allow_quarantined=allow_quarantined,
        )
    elif context.external_roots is not None:
        raise KernelError("process_profile_context_mismatch", "Process Profile规划上下文不匹配")
    argument_attributes: dict[str, object] = (
        {"cmd": arguments.cmd}
        if isinstance(arguments, RunCommandInput)
        else {"selectors": arguments.selectors}
    )
    resources: list[CanonicalActionResource] = [
        canonical_action_resource(
            kind="process",
            access="execute",
            identifier={"profile": owner.profile.profile_id},
            attributes={
                "profile_sha256": owner.profile.profile_sha256,
                "image_attestation_sha256": owner.image_attestation_sha256,
                "owner_capability_sha256": owner.runtime.capability.digest,
                **argument_attributes,
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
    workspace_resources = [WorkspaceResourceRequest(path=".", access="read")]
    for location in context.external_roots or {}:
        resources.extend(
            canonical_action_resource(
                kind="external",
                access=cast(Literal["read", "write"], access),
                identifier={"location": location, "path": "."},
                attributes={"target": "/harnessix/deps"},
            )
            for access in ("read", "write")
        )
        workspace_resources.extend(
            WorkspaceResourceRequest(
                location=location, path=".", access=cast(ResourceAccess, access)
            )
            for access in ("read", "write", "execute")
        )
    return ResolvedAction(
        resources=tuple(resources),
        workspace_resources=tuple(workspace_resources),
    )


def _process_execution(
    owner: VerifiedProductProcessProfile,
    route: ActionRoutePlan,
    arguments: RunProfileInput | RunCommandInput,
) -> ContainerExecutionSpec:
    """由固定Profile和批准参数确定性派生Container执行合同。"""

    profile = owner.profile
    supplied = (arguments.cmd,) if isinstance(arguments, RunCommandInput) else arguments.selectors
    argv = (profile.program, *profile.arguments, *supplied)
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
    arguments: RunProfileInput | RunCommandInput,
    dependency_environments: TaskDependencyEnvironments | None,
    *,
    allow_quarantined: bool = False,
) -> Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]] | None:
    """重新解析公共参数并核对Action Route所有安全绑定。"""

    external_roots = None
    if owner.profile.persistent_dependencies:
        if dependency_environments is None:
            raise KernelError("task_dependencies_unavailable", "任务依赖目录尚未绑定宿主Registry")
        external_roots = dependency_environments.for_route(
            route, owner.profile, allow_quarantined=allow_quarantined
        )
    resolved = resolve_run_profile(
        owner,
        arguments,
        ActionPlanningContext(
            workspace_root=workspace_root(route.execution.workspace.workspace_id),
            sandbox=owner.sandbox,
            capabilities=owner.capabilities,
            environment=owner.environment,
            secrets=owner.secret_bindings,
            external_roots=external_roots,
        ),
        dependency_environments=dependency_environments,
        allow_quarantined=allow_quarantined,
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
        or route.binding.executor_id != process_executor_id(owner.profile)
        or route.invocation.arguments != arguments.model_dump(mode="json")
        or route.resources != expected
        or route.execution.capabilities != owner.capabilities
        or route.execution.sandbox != owner.sandbox
        or route.execution.environment
        != bind_environment(owner.environment, platform=route.execution.workspace.platform)
        or route.execution.secrets != owner.secret_bindings
    ):
        raise KernelError("process_profile_plan_mismatch", "Process Profile与Action Route不匹配")
    return external_roots


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
        output=trusted_process_public_output(document, include_preview=True),
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
            "trusted_action_secret_unavailable",
            "process_output_protection_unavailable",
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
        *,
        dependency_environments: TaskDependencyEnvironments | None = None,
    ) -> None:
        if owner.profile.persistent_dependencies and dependency_environments is None:
            raise KernelError("task_dependencies_unavailable", "任务依赖目录尚未绑定宿主Registry")
        self.owner = owner
        self._router = router
        self._workspace_root = workspace_root
        self._secrets = secrets
        self._dependency_environments = dependency_environments

    def _quarantine_dependencies(self, route: ActionRoutePlan) -> None:
        if self.owner.profile.persistent_dependencies and self._dependency_environments is not None:
            self._dependency_environments.quarantine(route, self.owner.profile)

    def _protect_dependency_outcome(
        self,
        route: ActionRoutePlan,
        outcome: ActionExecutionOutcome,
        *,
        stop_reason: str | None = None,
    ) -> ActionExecutionOutcome:
        if outcome.kind in {"unknown", "manual_intervention"} or stop_reason in {
            "cancelled",
            "cleanup_failed",
            "host_lost",
            "unknown",
        }:
            self._quarantine_dependencies(route)
        return outcome

    async def execute(
        self,
        route: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        checked = _arguments(arguments, self.owner)
        external_roots = _validate_process_route(
            self.owner, self._workspace_root, route, checked, self._dependency_environments
        )
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
                    external_roots=external_roots,
                )
            outcome = await _process_lease_outcome(self.owner, lease, origin="execution")
        except asyncio.CancelledError:
            self._quarantine_dependencies(route)
            raise
        except KernelError as error:
            if error.code == "process_cleanup_failed":
                self._quarantine_dependencies(route)
            outcome = await _process_execution_failure(self.owner, route.execution.plan_id, error)
            return self._protect_dependency_outcome(route, outcome)
        except Exception:
            self._quarantine_dependencies(route)
            raise
        return self._protect_dependency_outcome(route, outcome, stop_reason=lease.stop_reason)

    async def reconcile(
        self,
        route: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        checked = _arguments(arguments, self.owner)
        _validate_process_route(
            self.owner,
            self._workspace_root,
            route,
            checked,
            self._dependency_environments,
            allow_quarantined=True,
        )
        execution = _process_execution(self.owner, route, checked)
        try:
            lease = await self.owner.runtime.reconcile(execution)
            outcome = await _process_lease_outcome(self.owner, lease, origin="recovery")
        except asyncio.CancelledError:
            self._quarantine_dependencies(route)
            raise
        except KernelError as error:
            if error.code == "process_lease_not_found":
                return ActionExecutionOutcome(kind="failed", error_code="process_not_started")
            self._quarantine_dependencies(route)
            return ActionExecutionOutcome(
                kind="manual_intervention",
                error_code="process_reconciliation_failed",
            )
        except Exception:
            self._quarantine_dependencies(route)
            raise
        return self._protect_dependency_outcome(route, outcome, stop_reason=lease.stop_reason)

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
        # 历史终态只按原Hash恢复v3、v2或v1，不能给旧审计事实追加新预览或重放Process。
        public = recorded_process_public_output(
            document,
            trusted_process_public_output(document, include_preview=True),
            expected_output_sha256,
        )
        if public is None or hashlib.sha256(body).hexdigest() != expected_artifact_sha256:
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
    *,
    dependency_environments: TaskDependencyEnvironments | None = None,
) -> tuple[TrustedActionDefinition, ProductProcessActionExecutor, ToolDescriptor]:
    """从同一动态Schema构造Descriptor、Router Binding与执行器。"""

    descriptor = process_profile_descriptor(owner.profile)
    binding = product_process_binding(owner.profile)
    executor = ProductProcessActionExecutor(
        owner, router, workspace_root, secrets, dependency_environments=dependency_environments
    )

    def execution_external_roots(
        route: ActionRoutePlan,
    ) -> Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]]:
        if dependency_environments is None:
            raise KernelError("task_dependencies_unavailable", "任务依赖目录尚未绑定宿主Registry")
        return dependency_environments.for_route(route, owner.profile)

    def decode(arguments: dict[str, JsonValue]) -> BaseModel:
        return _decode_arguments(owner.profile, arguments)

    def resolve(arguments: BaseModel, context: ActionPlanningContext) -> ResolvedAction:
        return resolve_run_profile(
            owner,
            _arguments(arguments, owner),
            context,
            dependency_environments=dependency_environments,
        )

    input_model = cast(
        type[BaseModel],
        RunCommandInput if owner.profile.selector_policy == "shell_command" else RunProfileInput,
    )
    definition = TrustedActionDefinition(
        binding=binding,
        input_model=input_model,
        resolve=resolve,
        executor=executor,
        input_schema=descriptor.input_schema,
        decode_arguments=decode,
        external_roots=execution_external_roots if owner.profile.persistent_dependencies else None,
    )
    return definition, executor, descriptor
