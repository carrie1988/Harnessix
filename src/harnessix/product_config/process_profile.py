"""固定Product Process Profile的容器、镜像、Owner与Secret能力探测。"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import (
    ExecutionCapabilityEvidenceV2,
    SandboxBindingV2,
    SecretVersionBinding,
    canonical_digest,
)
from harnessix.execution.planner import build_capability_evidence_v2
from harnessix.sandbox.capabilities import (
    ContainerEngineKind,
    ContainerEngineProbe,
    probe_container_engine,
)
from harnessix.sandbox.container import ContainerCommandBuilder, InspectRunner
from harnessix.sandbox.contracts import (
    ContainerSandboxProfile,
    NetworkPolicy,
    SandboxResourceLimits,
)
from harnessix.sandbox.network import resolve_network_policy
from harnessix.sandbox.planner import build_container_sandbox_profile
from harnessix.sandbox.process_runtime import ContainerProcessRuntime, ProcessSupervisor
from harnessix.secrets.provider import SecretProvider

from .action_contracts import ProductProcessProfile

ProfileProbeRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_IMAGE_PROBE_TIMEOUT_SECONDS = 15.0


def process_tool_name(profile_id: str) -> str:
    return f"run_profile.{profile_id}"


def _run_probe(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
        env={"PATH": os.defpath},
    )


def _engine(path: Path) -> ContainerEngineKind:
    name = path.name.casefold()
    if name.endswith(".exe"):
        name = name[:-4]
    if name not in {"docker", "podman"}:
        raise KernelError("container_engine_unsupported", "Process Profile容器引擎不受支持")
    return cast(ContainerEngineKind, name)


def _attest_image(
    path: Path,
    image: str,
    runner: ProfileProbeRunner,
) -> str:
    try:
        completed = runner(
            (
                str(path),
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                image,
            ),
            _IMAGE_PROBE_TIMEOUT_SECONDS,
        )
        if (
            completed.returncode != 0
            or len(completed.stdout.encode("utf-8")) > 64 * 1024
            or len(completed.stderr.encode("utf-8")) > 16 * 1024
        ):
            raise ValueError
        values = json.loads(completed.stdout)
        if (
            not isinstance(values, list)
            or image not in values
            or any(not isinstance(item, str) for item in values)
        ):
            raise ValueError
        canonical = tuple(sorted(set(values)))
    except (OSError, UnicodeError, ValueError, TypeError, subprocess.SubprocessError):
        raise KernelError(
            "container_image_unavailable", "Process Profile镜像摘要不可证明"
        ) from None
    return canonical_digest({"image": image, "repo_digests": canonical})


def _probe_secrets(profile: ProductProcessProfile, provider: SecretProvider) -> None:
    for reference in profile.secret_refs:
        if _ENVIRONMENT_NAME.fullmatch(reference.name) is None:
            raise KernelError("secret_target_invalid", "Process Profile Secret不能作为环境变量")
        material = provider.resolve(reference.name)
        try:
            if material.name != reference.name or material.version != reference.version:
                raise KernelError("secret_version_changed", "Process Profile Secret版本已经变化")
        finally:
            material.clear()


@dataclass(frozen=True, slots=True)
class VerifiedProductProcessProfile:
    """一次启动内可执行的Profile能力；不保存Secret明文。"""

    profile: ProductProcessProfile
    engine: ContainerEngineProbe
    image_attestation_sha256: str
    capabilities: ExecutionCapabilityEvidenceV2
    sandbox: SandboxBindingV2
    sandbox_profile: ContainerSandboxProfile
    runtime: ContainerProcessRuntime
    environment: Mapping[str, str]
    secret_bindings: tuple[SecretVersionBinding, ...]
    executor_evidence_sha256: str


@dataclass(frozen=True, slots=True)
class ProductProcessProfileProbeResult:
    """能力可用或诚实省略的互斥探测结果。"""

    profile: ProductProcessProfile
    reason_code: str
    verified: VerifiedProductProcessProfile | None = None


def _verified_product_process_profile(
    profile: ProductProcessProfile,
    supervisor: ProcessSupervisor,
    secrets: SecretProvider,
    *,
    probe_runner: ProfileProbeRunner | None,
    inspect_runner: InspectRunner | None,
) -> VerifiedProductProcessProfile:
    """完成Profile所有强能力证明并装配单一Container Process Runtime。"""

    if profile.memory_bytes < 64 * 1024 * 1024 or profile.process_limit < 16:
        raise KernelError("profile_limits_unenforceable", "Process Profile资源限制无法执行")
    path = Path(profile.container_engine)
    engine_kind = _engine(path)
    engine = (
        probe_container_engine(path, engine=engine_kind)
        if probe_runner is None
        else probe_container_engine(path, engine=engine_kind, runner=probe_runner)
    )
    if engine.platform != supervisor.capability.platform:
        raise KernelError("process_owner_mismatch", "容器引擎与Process Owner平台不一致")
    image_attestation = _attest_image(path, profile.image, probe_runner or _run_probe)
    _probe_secrets(profile, secrets)
    limits = SandboxResourceLimits(
        cpus=profile.cpu_limit,
        memory_bytes=profile.memory_bytes,
        pids=profile.process_limit,
        tmpfs_bytes=64 * 1024 * 1024,
    )
    network = resolve_network_policy(NetworkPolicy(mode="none"))
    sandbox_profile = build_container_sandbox_profile(
        image=profile.image,
        workspace_mode="read_only",
        network=network,
        limits=limits,
    )
    capabilities = build_capability_evidence_v2(
        platform=engine.platform,
        provider=engine.engine,
        provider_version=engine.server_version,
        sandbox_levels=("container_strong",),
        network_modes=("none",),
        supports_pty=False,
        supports_background=True,
        supports_process_tree=True,
        provider_evidence_digest=engine.digest,
    )
    sandbox = SandboxBindingV2(
        level="container_strong",
        backend=engine.engine,
        backend_version=engine.server_version,
        network="none",
        capability_digest=capabilities.evidence_digest,
        profile_digest=sandbox_profile.digest,
    )
    builder = (
        ContainerCommandBuilder(path, engine)
        if inspect_runner is None
        else ContainerCommandBuilder(path, engine, inspect_runner=inspect_runner)
    )
    runtime = ContainerProcessRuntime(builder, supervisor)
    bindings = tuple(
        SecretVersionBinding(name=reference.name, version=reference.version, target=reference.name)
        for reference in profile.secret_refs
    )
    environment = {"LANG": "C", "LC_ALL": "C"}
    evidence = canonical_digest(
        {
            "spec_version": "harnessix.product-process-executor-evidence/v1",
            "profile_sha256": profile.profile_sha256,
            "engine_probe_sha256": engine.digest,
            "image_attestation_sha256": image_attestation,
            "owner_capability_sha256": supervisor.capability.digest,
            "sandbox_profile_sha256": sandbox_profile.digest,
            "environment": environment,
            "secrets": [item.model_dump(mode="json") for item in bindings],
        }
    )
    return VerifiedProductProcessProfile(
        profile=profile,
        engine=engine,
        image_attestation_sha256=image_attestation,
        capabilities=capabilities,
        sandbox=sandbox,
        sandbox_profile=sandbox_profile,
        runtime=runtime,
        environment=MappingProxyType(environment),
        secret_bindings=bindings,
        executor_evidence_sha256=evidence,
    )


def probe_product_process_profile(
    profile: ProductProcessProfile,
    supervisor: ProcessSupervisor,
    secrets: SecretProvider,
    *,
    probe_runner: ProfileProbeRunner | None = None,
    inspect_runner: InspectRunner | None = None,
) -> ProductProcessProfileProbeResult:
    """只有全部强能力证明成立才返回可执行Owner；失败不创建Host降级路径。"""

    try:
        verified = _verified_product_process_profile(
            profile,
            supervisor,
            secrets,
            probe_runner=probe_runner,
            inspect_runner=inspect_runner,
        )
    except KernelError as error:
        reasons = {
            "container_engine_unsupported": "container_engine_unsupported",
            "sandbox_unavailable": "container_unavailable",
            "sandbox_binding_invalid": "container_unavailable",
            "container_image_unavailable": "container_image_unavailable",
            "profile_limits_unenforceable": "profile_limits_unenforceable",
            "process_owner_mismatch": "process_owner_unavailable",
            "secret_target_invalid": "secret_target_invalid",
            "secret_unavailable": "secret_unavailable",
            "secret_version_changed": "secret_version_changed",
            "secret_provider_invalid": "secret_unavailable",
        }
        return ProductProcessProfileProbeResult(
            profile=profile,
            reason_code=reasons.get(error.code, "profile_probe_failed"),
        )
    return ProductProcessProfileProbeResult(
        profile=profile,
        reason_code="verified",
        verified=verified,
    )
