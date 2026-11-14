"""原生进程Profile：不经容器执行Shell命令。

- 外部边界（ADR 0121）：隔离由启动Harnessix的一方提供；只有配置显式声明且App Server环境断言
  `HARNESSIX_EXTERNAL_BOUNDARY=1`时才装配。
- 本机沙箱（ADR 0111）：bubblewrap在启动时探测并推导策略，计划记为`host_sandboxed`。
任何条件不成立都省略工具并给出原因码，不降级为其他执行方式。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

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
from harnessix.sandbox.native_policy import host_environment
from harnessix.sandbox.native_probe import probe_native_sandbox
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
from .native_sandbox import (
    NativeSandboxBinding,
    NativeSandboxSite,
    SandboxProbe,
    attest_native_sandbox,
    prepare_sandboxed_argv,
)
from .native_shells import native_owner_problem, shell_command_text
from .process_command import (
    HostCommandInput,
    RunCommandInput,
    decode_host_command,
    decode_run_command,
    host_command_schema,
    run_command_schema,
)
from .process_profile import process_tool_name

EXTERNAL_BOUNDARY_ENVIRONMENT = "HARNESSIX_EXTERNAL_BOUNDARY"
NATIVE_COMMAND_VERSION = "harnessix.native-command/v2"
HOST_COMMAND_VERSION = "harnessix.host-command/v2"
HOST_COMMAND_PREFIX = "host_command"
# 命令的基础环境；Profile预设同名变量时以预设为准。
_BASE_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
)
WorkspaceRootResolver = Callable[[str], Path]
# 沙箱启动前即可确定、不会产生副作用的失败（ADR 0111第5节）。
_SANDBOX_PREFLIGHT_CODES = frozenset(
    {
        "sandbox_backend_changed",
        "sandbox_path_invalid",
        "sandbox_policy_too_large",
        "workspace_secret_files_unmaskable",
    }
)


@dataclass(frozen=True, slots=True)
class AttestedNativeProcessProfile:
    """不创建Process Store即可证明的执行边界，供诊断与运行时共用。"""

    profile: ProductNativeProcessProfile
    capabilities: ExecutionCapabilityEvidenceV2
    sandbox: SandboxBindingV2
    environment: Mapping[str, str]
    executor_evidence_sha256: str
    # 仅本机沙箱后端；外部边界为None。
    native_sandbox: NativeSandboxBinding | None


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
    """原生与容器命令Profile同属命令类身份（ID互不重复），公开输出合同与启动恢复按此识别。

    沙箱外命令（ADR 0113）用独立身份，不被`exec_command.`前缀的自动批准规则覆盖。
    """

    if profile.backend == "host":
        return f"product.host-command.{profile.profile_id}"
    return f"product.command-profile.{profile.profile_id}"


def native_tool_name(profile: ProductNativeProcessProfile) -> str:
    if profile.backend == "host":
        return f"{HOST_COMMAND_PREFIX}.{profile.profile_id}"
    return process_tool_name(profile.profile_id, "shell_command")


def attest_native_process_profile(
    profile: ProductNativeProcessProfile,
    owner_capability: ProcessCapabilityProbe | None,
    *,
    environ: Mapping[str, str],
    site: NativeSandboxSite | None = None,
    secret_names: tuple[str, ...] = (),
    sandbox_probe: SandboxProbe = probe_native_sandbox,
) -> tuple[str, AttestedNativeProcessProfile | None]:
    """按后端证明执行边界；返回原因码与证明，不成立时不降级。"""

    problem = native_owner_problem(owner_capability, profile.backend)
    if problem is not None or owner_capability is None:
        return problem or "process_owner_unavailable", None
    if profile.backend == "host":
        return "verified", _attest_host(profile, owner_capability, environ, secret_names)
    if profile.backend != "external_boundary":
        if site is None:
            return "native_sandbox_site_unavailable", None
        return _attest_sandboxed(
            profile,
            owner_capability,
            environ=environ,
            site=site,
            secret_names=secret_names,
            sandbox_probe=sandbox_probe,
        )
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
        native_sandbox=None,
    )


def _attest_host(
    profile: ProductNativeProcessProfile,
    owner_capability: ProcessCapabilityProbe,
    environ: Mapping[str, str],
    secret_names: tuple[str, ...],
) -> AttestedNativeProcessProfile:
    """沙箱外命令：如实记为host_guarded/host/full；每条都需用户批准（ADR 0113第2.2节）。"""

    environment = host_environment(environ, presets=profile.environment, secret_names=secret_names)
    capabilities = build_capability_evidence_v2(
        platform=owner_capability.platform,
        provider="host",
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
                "spec_version": "harnessix.native-host-profile/v1",
                "profile_sha256": profile.profile_sha256,
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
    return AttestedNativeProcessProfile(
        profile=profile,
        capabilities=capabilities,
        sandbox=sandbox,
        environment=environment,
        executor_evidence_sha256=evidence,
        native_sandbox=None,
    )


def _attest_sandboxed(
    profile: ProductNativeProcessProfile,
    owner_capability: ProcessCapabilityProbe,
    *,
    environ: Mapping[str, str],
    site: NativeSandboxSite,
    secret_names: tuple[str, ...],
    sandbox_probe: SandboxProbe,
) -> tuple[str, AttestedNativeProcessProfile | None]:
    reason, attested = attest_native_sandbox(
        profile,
        owner_capability,
        environ=environ,
        site=site,
        secret_names=secret_names,
        probe=sandbox_probe,
    )
    if attested is None:
        return reason, None
    evidence = canonical_digest(
        {
            "spec_version": "harnessix.product-native-executor-evidence/v1",
            "profile_sha256": profile.profile_sha256,
            "capability_sha256": attested.capabilities.evidence_digest,
            "sandbox_sha256": canonical_digest(attested.sandbox.model_dump(mode="json")),
        }
    )
    return "verified", AttestedNativeProcessProfile(
        profile=profile,
        capabilities=attested.capabilities,
        sandbox=attested.sandbox,
        environment=attested.environment,
        executor_evidence_sha256=evidence,
        native_sandbox=attested.binding,
    )


def probe_native_process_profile(
    profile: ProductNativeProcessProfile,
    supervisor: object,
    *,
    environ: Mapping[str, str],
    site: NativeSandboxSite | None = None,
    secret_names: tuple[str, ...] = (),
    sandbox_probe: SandboxProbe = probe_native_sandbox,
) -> NativeProcessProfileProbeResult:
    """证明成立且Owner为POSIX Process Owner时返回可执行Profile，否则诚实省略。"""

    if not isinstance(supervisor, PosixProcessSupervisor):
        return NativeProcessProfileProbeResult(profile, "process_owner_unavailable")
    reason, attested = attest_native_process_profile(
        profile,
        supervisor.capability,
        environ=environ,
        site=site,
        secret_names=secret_names,
        sandbox_probe=sandbox_probe,
    )
    if attested is None:
        return NativeProcessProfileProbeResult(profile, reason)
    verified = VerifiedNativeProcessProfile(
        profile=attested.profile,
        capabilities=attested.capabilities,
        sandbox=attested.sandbox,
        environment=attested.environment,
        executor_evidence_sha256=attested.executor_evidence_sha256,
        native_sandbox=attested.native_sandbox,
        supervisor=supervisor,
    )
    return NativeProcessProfileProbeResult(profile, "verified", verified)


def native_process_descriptor(profile: ProductNativeProcessProfile) -> ToolDescriptor:
    host = profile.backend == "host"
    return ToolDescriptor(
        name=native_tool_name(profile),
        version=f"{HOST_COMMAND_VERSION if host else NATIVE_COMMAND_VERSION}:"
        f"{profile.profile_sha256[:24]}",
        description=(
            f"在当前环境中执行Shell命令“{profile.description}”（{profile.profile_id} "
            f'{profile.version}）；必填"cmd"'
            + ('和"reason"' if host else "")
            + f'，"profile"可省略（工具名已确定，传入时须为"{profile.profile_id}"）。'
            + "cmd必须非空白，允许管道、重定向和多行，不含NUL且最多16384 UTF-8字节。"
            + _boundary_description(profile)
            + (
                "预设环境变量：" + "、".join(name for name, _ in profile.environment) + "。"
                if profile.environment
                else ""
            )
            + "失败先看diagnostic_preview；完整时不重读日志Artifact。"
        ),
        input_schema=(
            host_command_schema(profile.profile_id)
            if host
            else run_command_schema(profile.profile_id)
        ),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        requires_idempotency=True,
        requires_approval=True,
        supports_reconciliation=True,
        supports_parallel_calls=False,
    )


def _boundary_description(profile: ProductNativeProcessProfile) -> str:
    """如实写明边界（含TMPDIR每条命令前清空），模型不必试错；外部边界文本与指纹不变。"""

    if profile.backend == "external_boundary":
        return (
            "命令从工作区根目录开始，可直接读写工作区，改动会保留；"
            "Harnessix不另加沙箱，隔离与网络由外部环境决定。"
        )
    if profile.backend == "host":
        return (
            "命令在沙箱外从工作区根目录开始运行：没有隔离，可以联网，可以读写用户目录；"
            "每条命令都需要用户批准，reason会展示给用户。只在沙箱内的命令因需要联网"
            "（如安装依赖、下载）或写工作区以外位置而无法完成时使用；其余命令请用沙箱内的命令工具。"
        )
    workspace = (
        "可直接读写工作区，改动会保留" if profile.workspace_mode == "read_write" else "工作区只读"
    )
    return (
        f"命令在本机沙箱中从工作区根目录开始运行，{workspace}；"
        "可读整台机器，但凭据、命令历史、Harnessix状态与工作区.env文件不可读；"
        "只能写工作区、TMPDIR与私有缓存目录，HOME不可写；"
        "TMPDIR在每条命令开始前清空，后面的命令还要用的文件（如编译产物、测试夹具）请放在工作区里；"
        ".git只读，提交与暂存由Harnessix交付流程完成；没有网络，联网操作会失败。"
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


def _decode(
    profile: ProductNativeProcessProfile, arguments: dict[str, JsonValue]
) -> RunCommandInput:
    if profile.backend == "host":
        return decode_host_command(profile.profile_id, arguments)
    return decode_run_command(profile.profile_id, arguments)


def _checked_arguments(value: BaseModel, owner: VerifiedNativeProcessProfile) -> RunCommandInput:
    try:
        dumped = cast(dict[str, JsonValue], value.model_dump(mode="json"))
        return _decode(owner.profile, dumped)
    except (ValueError, TypeError):
        raise KernelError(
            "process_profile_arguments_invalid", "Process Profile参数类型不一致"
        ) from None


def resolve_native_command(
    owner: VerifiedNativeProcessProfile,
    arguments: RunCommandInput,
    context: ActionPlanningContext,
) -> ResolvedAction:
    """绑定Profile、Owner能力、工作区访问、额外可写目录与网络（沙箱断网时不声明网络资源）。"""

    if (
        context.cwd != "."
        or context.capabilities != owner.capabilities
        or context.sandbox != owner.sandbox
        or dict(context.environment) != dict(owner.environment)
        or context.secrets
        or context.external_roots is not None
    ):
        raise KernelError("process_profile_context_mismatch", "Process Profile规划上下文不匹配")
    profile = owner.profile
    access: Literal["read", "write"] = "write" if profile.workspace_mode == "read_write" else "read"
    resources = [
        canonical_action_resource(
            kind="process",
            access="execute",
            identifier={"profile": profile.profile_id},
            attributes={
                "profile_sha256": profile.profile_sha256,
                "owner_capability_sha256": owner.supervisor.capability.digest,
                "cmd": arguments.cmd,
                **({"reason": arguments.reason} if isinstance(arguments, HostCommandInput) else {}),
            },
        ),
        canonical_action_resource(
            kind="workspace",
            access=access,
            identifier={"location": "workspace", "path": "."},
            attributes={"mount": profile.backend},
        ),
    ]
    resources.extend(
        canonical_action_resource(
            kind="external",
            access="write",
            identifier={"location": "native-write-root", "path": root},
            attributes={"mount": profile.backend},
        )
        for root in profile.write_roots
    )
    if owner.sandbox.network != "none":
        resources.append(
            canonical_action_resource(
                kind="network",
                access="connect",
                identifier={"policy": owner.sandbox.network},
                attributes={"enforcement": profile.backend},
            )
        )
    return ResolvedAction(
        resources=tuple(resources),
        workspace_resources=(WorkspaceResourceRequest(path=".", access=access),),
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
        or route.execution.environment
        != bind_environment(owner.environment, platform=owner.capabilities.platform)
        or route.execution.secrets
    ):
        raise KernelError("native_process_plan_mismatch", "原生Process Profile与Action Route不匹配")


def _process_spec(
    owner: VerifiedNativeProcessProfile, route: ActionRoutePlan, arguments: RunCommandInput
) -> ProcessSpec:
    profile = owner.profile
    command = (profile.program, *profile.arguments, shell_command_text(profile, arguments.cmd))
    if owner.native_sandbox is not None:
        command = prepare_sandboxed_argv(owner.native_sandbox, command)
    return build_process_spec(
        invocation="argv",
        argv=command,
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
        preflight_codes=frozenset(
            {"native_process_plan_mismatch", "product_workspace_mismatch"}
            | _SANDBOX_PREFLIGHT_CODES
        ),
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
        return _decode(owner.profile, arguments)

    def resolve(arguments: BaseModel, context: ActionPlanningContext) -> ResolvedAction:
        return resolve_native_command(owner, _checked_arguments(arguments, owner), context)

    definition = TrustedActionDefinition(
        binding=binding,
        input_model=HostCommandInput if owner.profile.backend == "host" else RunCommandInput,
        resolve=resolve,
        executor=executor,
        input_schema=descriptor.input_schema,
        decode_arguments=decode,
    )
    return definition, executor, descriptor
