"""原生进程Profile（ADR 0121）：不经容器、在当前环境直接执行Shell命令。

本期只有外部边界后端：隔离由启动Harnessix的一方提供（例如评测任务容器），Harnessix不另加沙箱。
只有配置显式声明且App Server环境断言`HARNESSIX_EXTERNAL_BOUNDARY=1`时才装配；否则省略工具，不降级。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast

from pydantic import BaseModel, JsonValue

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.execution.contracts import (
    ExecutionCapabilityEvidenceV2,
    SandboxBindingV2,
    canonical_digest,
)
from harnessix.execution.planner import bind_environment, build_capability_evidence_v2
from harnessix.processes.supervision_contracts import ProcessCapabilityProbe, ProcessSpec
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.processes.trusted_output import trusted_process_public_output
from harnessix.trusted_actions.contracts import (
    ActionRoutePlan,
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

from .action_contracts import ProductNativeProcessProfile
from .host_process_action import HostProcessActionExecutor, HostProcessBinding
from .process_command import RunCommandInput, decode_run_command, run_command_schema
from .process_profile import process_tool_name

EXTERNAL_BOUNDARY_ENVIRONMENT = "HARNESSIX_EXTERNAL_BOUNDARY"
NATIVE_COMMAND_VERSION = "harnessix.native-command/v1"
# 命令的基础环境；Profile预设同名变量时以预设为准。
_BASE_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
)
WorkspaceRootResolver = Callable[[str], Path]


@dataclass(frozen=True, slots=True)
class AttestedNativeProcessProfile:
    """不创建Process Store即可证明的执行边界，供诊断与运行时共用。"""

    profile: ProductNativeProcessProfile
    capabilities: ExecutionCapabilityEvidenceV2
    sandbox: SandboxBindingV2
    environment: Mapping[str, str]
    executor_evidence_sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedNativeProcessProfile(AttestedNativeProcessProfile):
    """一次启动内已绑定POSIX Process Owner、可执行的原生命令Profile。"""

    supervisor: PosixProcessSupervisor


@dataclass(frozen=True, slots=True)
class NativeProcessProfileProbeResult:
    """能力可用或诚实省略的互斥探测结果。"""

    profile: ProductNativeProcessProfile
    reason_code: str
    verified: VerifiedNativeProcessProfile | None = None


def native_process_executor_id(profile: ProductNativeProcessProfile) -> str:
    """原生与容器命令Profile同属命令类身份（ID互不重复），公开输出合同与启动恢复按此识别。"""

    return f"product.command-profile.{profile.profile_id}"


def attest_native_process_profile(
    profile: ProductNativeProcessProfile,
    owner_capability: ProcessCapabilityProbe | None,
    *,
    environ: Mapping[str, str],
) -> tuple[str, AttestedNativeProcessProfile | None]:
    """外部边界只在POSIX Owner可用且启动方显式断言边界时成立；返回原因码与证明。"""

    if profile.backend != "external_boundary":
        # 沙箱与沙箱外后端（ADR 0111、0113）在接入执行器前一律诚实省略，绝不按外部边界执行。
        return "native_backend_unsupported", None
    if owner_capability is None or owner_capability.platform != "posix":
        return "process_owner_unavailable", None
    if environ.get(EXTERNAL_BOUNDARY_ENVIRONMENT) != "1":
        return "external_boundary_unasserted", None
    environment = MappingProxyType({**_BASE_ENVIRONMENT, **dict(profile.environment)})
    capabilities = build_capability_evidence_v2(
        platform="posix",
        provider="external-boundary",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("full",),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
        provider_evidence_digest=owner_capability.digest,
    )
    sandbox = SandboxBindingV2(
        level="host_guarded",
        backend="host",
        backend_version="1",
        network="full",
        capability_digest=capabilities.evidence_digest,
        profile_digest=canonical_digest(
            {
                "spec_version": "harnessix.native-boundary-profile/v1",
                "profile_sha256": profile.profile_sha256,
                "boundary": profile.backend,
                "owner_capability_sha256": owner_capability.digest,
                "environment": dict(environment),
            }
        ),
    )
    evidence = canonical_digest(
        {
            "spec_version": "harnessix.product-native-executor-evidence/v1",
            "profile_sha256": profile.profile_sha256,
            "capability_sha256": capabilities.evidence_digest,
            "sandbox_sha256": canonical_digest(sandbox.model_dump(mode="json")),
        }
    )
    return "verified", AttestedNativeProcessProfile(
        profile=profile,
        capabilities=capabilities,
        sandbox=sandbox,
        environment=environment,
        executor_evidence_sha256=evidence,
    )


def probe_native_process_profile(
    profile: ProductNativeProcessProfile,
    supervisor: object,
    *,
    environ: Mapping[str, str],
) -> NativeProcessProfileProbeResult:
    """证明成立且Owner为POSIX Process Owner时返回可执行Profile，否则诚实省略。"""

    if not isinstance(supervisor, PosixProcessSupervisor):
        return NativeProcessProfileProbeResult(profile, "process_owner_unavailable")
    reason, attested = attest_native_process_profile(
        profile, supervisor.capability, environ=environ
    )
    if attested is None:
        return NativeProcessProfileProbeResult(profile, reason)
    verified = VerifiedNativeProcessProfile(
        profile=attested.profile,
        capabilities=attested.capabilities,
        sandbox=attested.sandbox,
        environment=attested.environment,
        executor_evidence_sha256=attested.executor_evidence_sha256,
        supervisor=supervisor,
    )
    return NativeProcessProfileProbeResult(profile, "verified", verified)


def native_process_descriptor(profile: ProductNativeProcessProfile) -> ToolDescriptor:
    return ToolDescriptor(
        name=process_tool_name(profile.profile_id, "shell_command"),
        version=f"{NATIVE_COMMAND_VERSION}:{profile.profile_sha256[:24]}",
        description=(
            f"在当前环境中执行Shell命令“{profile.description}”（{profile.profile_id} "
            f'{profile.version}）；必填"profile": "{profile.profile_id}"和"cmd"，'
            "工具名不能替代字段。cmd必须非空白，允许管道、重定向和多行，"
            "不含NUL且最多16384 UTF-8字节。"
            "命令从工作区根目录开始，可直接读写工作区，改动会保留；"
            "Harnessix不另加沙箱，隔离与网络由外部环境决定。"
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


def native_process_binding(profile: ProductNativeProcessProfile) -> TrustedToolBinding:
    """从固定Profile构造稳定Binding，供运行时和无状态诊断共同复用。"""

    descriptor = native_process_descriptor(profile)
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
        executor_id=native_process_executor_id(profile),
    )


def _checked_arguments(value: BaseModel, owner: VerifiedNativeProcessProfile) -> RunCommandInput:
    try:
        dumped = cast(dict[str, JsonValue], value.model_dump(mode="json"))
        return decode_run_command(owner.profile.profile_id, dumped)
    except (ValueError, TypeError):
        raise KernelError(
            "process_profile_arguments_invalid", "Process Profile参数类型不一致"
        ) from None


def resolve_native_command(
    owner: VerifiedNativeProcessProfile,
    arguments: RunCommandInput,
    context: ActionPlanningContext,
) -> ResolvedAction:
    """绑定Profile、Owner能力、工作区写权限与由外部边界决定的网络。"""

    if (
        context.cwd != "."
        or context.capabilities != owner.capabilities
        or context.sandbox != owner.sandbox
        or dict(context.environment) != dict(owner.environment)
        or context.secrets
        or context.external_roots is not None
    ):
        raise KernelError("process_profile_context_mismatch", "Process Profile规划上下文不匹配")
    return ResolvedAction(
        resources=(
            canonical_action_resource(
                kind="process",
                access="execute",
                identifier={"profile": owner.profile.profile_id},
                attributes={
                    "profile_sha256": owner.profile.profile_sha256,
                    "owner_capability_sha256": owner.supervisor.capability.digest,
                    "cmd": arguments.cmd,
                },
            ),
            canonical_action_resource(
                kind="workspace",
                access="write",
                identifier={"location": "workspace", "path": "."},
                attributes={"mount": owner.profile.backend},
            ),
            canonical_action_resource(
                kind="network",
                access="connect",
                identifier={"policy": "full"},
                attributes={"enforcement": owner.profile.backend},
            ),
        ),
        workspace_resources=(WorkspaceResourceRequest(path=".", access="write"),),
    )


def _validate_route(
    owner: VerifiedNativeProcessProfile,
    workspace_root: WorkspaceRootResolver,
    route: ActionRoutePlan,
    arguments: RunCommandInput,
) -> None:
    """重新解析公共参数并核对Action Route的全部安全绑定。"""

    resolved = resolve_native_command(
        owner,
        arguments,
        ActionPlanningContext(
            workspace_root=workspace_root(route.execution.workspace.workspace_id),
            sandbox=owner.sandbox,
            capabilities=owner.capabilities,
            environment=owner.environment,
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
    descriptor = native_process_descriptor(owner.profile)
    if (
        route.binding.tool != descriptor.name
        or route.binding.executor_id != native_process_executor_id(owner.profile)
        or route.invocation.arguments != arguments.model_dump(mode="json")
        or route.resources != expected
        or route.execution.capabilities != owner.capabilities
        or route.execution.sandbox != owner.sandbox
        or route.execution.environment != bind_environment(owner.environment, platform="posix")
        or route.execution.secrets
    ):
        raise KernelError("native_process_plan_mismatch", "原生Process Profile与Action Route不匹配")


def _process_spec(
    owner: VerifiedNativeProcessProfile, route: ActionRoutePlan, arguments: RunCommandInput
) -> ProcessSpec:
    profile = owner.profile
    return build_process_spec(
        invocation="argv",
        argv=(profile.program, *profile.arguments, arguments.cmd),
        terminal="pipe",
        stdin="closed",
        lifecycle="foreground",
        timeout_seconds=profile.timeout_seconds,
        output_bytes=profile.max_output_bytes,
        input_bytes=0,
        process_id=route.execution.plan_id,
    )


def _binding(
    owner: VerifiedNativeProcessProfile, workspace_root: WorkspaceRootResolver
) -> HostProcessBinding:
    """与容器命令工具一致：非零退出码报为失败，输出带诊断预览。"""

    return HostProcessBinding(
        label=owner.profile.profile_id,
        supervisor=owner.supervisor,
        environment=owner.environment,
        check_arguments=lambda value: _checked_arguments(value, owner),
        validate_route=lambda route, arguments: _validate_route(
            owner, workspace_root, route, cast(RunCommandInput, arguments)
        ),
        process_spec=lambda route, arguments: _process_spec(
            owner, route, cast(RunCommandInput, arguments)
        ),
        workspace=lambda route: workspace_root(route.execution.workspace.workspace_id),
        public_output=lambda document: trusted_process_public_output(
            document, include_preview=True
        ),
        nonzero_exit_fails=True,
        preflight_codes=frozenset({"native_process_plan_mismatch", "product_workspace_mismatch"}),
    )


def build_native_process_definition(
    owner: VerifiedNativeProcessProfile,
    router: TrustedActionRouter,
    workspace_root: WorkspaceRootResolver,
) -> tuple[TrustedActionDefinition, HostProcessActionExecutor, ToolDescriptor]:
    """从同一Schema构造Descriptor、Router Binding与执行器。"""

    descriptor = native_process_descriptor(owner.profile)
    binding = native_process_binding(owner.profile)
    executor = HostProcessActionExecutor(_binding(owner, workspace_root), router)

    def decode(arguments: dict[str, JsonValue]) -> BaseModel:
        return decode_run_command(owner.profile.profile_id, arguments)

    def resolve(arguments: BaseModel, context: ActionPlanningContext) -> ResolvedAction:
        return resolve_native_command(owner, _checked_arguments(arguments, owner), context)

    definition = TrustedActionDefinition(
        binding=binding,
        input_model=RunCommandInput,
        resolve=resolve,
        executor=executor,
        input_schema=descriptor.input_schema,
        decode_arguments=decode,
    )
    return definition, executor, descriptor
