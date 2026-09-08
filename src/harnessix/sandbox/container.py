from __future__ import annotations

import csv
import io
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import (
    ExecutionApprovalCheckpoint,
    ExecutionPlanV2,
    execution_is_approved,
)
from harnessix.execution.planner import bind_environment
from harnessix.sandbox.capabilities import ContainerEngineProbe, executable_identity_digest
from harnessix.sandbox.contracts import (
    ContainerCommandSpec,
    ContainerSandboxProfile,
    ManagedEgressBinding,
    NetworkPolicySnapshot,
)
from harnessix.secrets.provider import ResolvedSecretEnvironment
from harnessix.tools.contracts import Revision
from harnessix.workspace.contracts import ResourceAccess
from harnessix.workspace.snapshot import verify_workspace_snapshot


@dataclass(frozen=True, slots=True)
class PreparedContainerLaunch:
    argv: tuple[str, ...]
    base_environment: Mapping[str, str] = field(repr=False)
    secrets: ResolvedSecretEnvironment | None = field(repr=False)
    plan_fingerprint: Revision
    profile_digest: Revision

    def materialize_environment(self) -> dict[str, str]:
        values = dict(self.base_environment)
        if self.secrets is not None:
            values.update(self.secrets.as_text())
        return values

    def redaction_values(self) -> tuple[bytes, ...]:
        return () if self.secrets is None else self.secrets.raw_values()


def _file_identity(path: Path) -> tuple[int, ...]:
    info = path.stat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _csv_fields(*values: str) -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(values)
    return output.getvalue()


class ContainerCommandBuilder:
    """只生成固定Docker兼容argv；启动和回收由Process Supervisor拥有。"""

    def __init__(self, executable: str | Path, probe: ContainerEngineProbe) -> None:
        path = Path(executable)
        try:
            if not path.is_absolute():
                raise ValueError
            path = path.resolve(strict=True)
            identity = _file_identity(path)
            if (
                not stat.S_ISREG(identity[-1])
                or not os.access(path, os.X_OK)
                or executable_identity_digest(path) != probe.executable_identity
            ):
                raise ValueError
        except (OSError, ValueError):
            raise KernelError("sandbox_binding_invalid", "容器引擎绑定无效") from None
        self._path = path
        self._identity = identity
        self._engine = probe.engine
        self._version = probe.server_version
        self._capability_digest = probe.digest

    def prepare(
        self,
        plan: ExecutionPlanV2,
        checkpoint: ExecutionApprovalCheckpoint | None,
        profile: ContainerSandboxProfile,
        *,
        workspace: str | Path,
        command: ContainerCommandSpec,
        environment: Mapping[str, str],
        secrets: ResolvedSecretEnvironment | None = None,
        external_roots: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]] | None = None,
        egress: ManagedEgressBinding | None = None,
    ) -> PreparedContainerLaunch:
        self._verify_binding()
        if not execution_is_approved(plan, checkpoint):
            raise KernelError("approval_required", "Execution Plan尚未获得有效批准")
        if (
            plan.sandbox.level != "container_strong"
            or plan.sandbox.backend != self._engine
            or plan.sandbox.backend_version != self._version
            or plan.capabilities.provider_evidence_digest != self._capability_digest
            or plan.sandbox.profile_digest != profile.digest
            or plan.sandbox.network != profile.network.policy.mode
            or command.profile_digest != profile.digest
            or plan.intent.arguments != command.model_dump(mode="json", warnings="error")
        ):
            raise KernelError("sandbox_capability_mismatch", "Execution Plan与容器后端不匹配")
        try:
            checked_environment = dict(environment)
            if (
                len(checked_environment) > 128
                or any(
                    type(name) is not str
                    or type(value) is not str
                    or not name
                    or "=" in name
                    or "\0" in name
                    or "\0" in value
                    for name, value in checked_environment.items()
                )
                or sum(
                    len(name.encode()) + len(value.encode()) + 2
                    for name, value in checked_environment.items()
                )
                > 65536
            ):
                raise ValueError
        except (UnicodeError, ValueError, TypeError):
            raise KernelError("sandbox_environment_invalid", "容器环境不符合契约") from None
        if (
            bind_environment(checked_environment, platform=plan.workspace.platform)
            != plan.environment
        ):
            raise KernelError("execution_plan_stale", "容器环境与Execution Plan不一致")
        secret_environment = {} if secrets is None else secrets.as_text()
        comparison = str.casefold if plan.workspace.platform == "windows" else lambda value: value
        actual_secret_bindings = () if secrets is None else secrets.bindings()
        expected_secret_bindings = tuple(
            sorted((binding.target, binding.name, binding.version) for binding in plan.secrets)
        )
        normalized_actual = {
            (comparison(target), name, version) for target, name, version in actual_secret_bindings
        }
        normalized_expected = {
            (comparison(target), name, version)
            for target, name, version in expected_secret_bindings
        }
        if normalized_actual != normalized_expected:
            raise KernelError("secret_binding_mismatch", "Secret注入目标与Execution Plan不一致")
        root = Path(workspace)
        verify_workspace_snapshot(plan.workspace, root, external_roots=external_roots)
        if not root.is_absolute():
            raise KernelError("sandbox_workspace_invalid", "容器Workspace必须是绝对路径")
        if plan.workspace.external_roots:
            raise KernelError("sandbox_capability_mismatch", "容器后端尚未声明外部根挂载能力")
        required_access = "read" if profile.workspace_mode == "read_only" else "write"
        if not any(
            item.location == "workspace"
            and item.path == "."
            and item.access == required_access
            and item.kind == "directory"
            for item in plan.workspace.resources
        ):
            raise KernelError("sandbox_capability_mismatch", "容器根挂载缺少对应Permission")
        network_arguments, proxy_environment = self._network(
            profile.network, profile.egress_gateway_digest, egress
        )
        overlap = (set(checked_environment) | set(secret_environment)) & set(proxy_environment)
        if overlap:
            raise KernelError("sandbox_environment_invalid", "容器环境覆盖了受管网络配置")
        checked_environment.update(proxy_environment)
        mount = _csv_fields(
            "type=bind",
            f"src={root}",
            "dst=/workspace",
            *("readonly",) if profile.workspace_mode == "read_only" else (),
        )
        arguments = [
            str(self._path),
            "run",
            "--rm",
            "--init",
            "--pull",
            "never",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--ipc",
            "none",
            "--pids-limit",
            str(profile.limits.pids),
            "--memory",
            str(profile.limits.memory_bytes),
            "--cpus",
            format(profile.limits.cpus, "g"),
            "--user",
            profile.run_as,
            "--workdir",
            "/workspace" if plan.workspace.cwd == "." else "/workspace/" + plan.workspace.cwd,
            "--mount",
            mount,
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={profile.limits.tmpfs_bytes}",
            "--ulimit",
            "nofile=1024:1024",
            "--stop-timeout",
            "5",
            *network_arguments,
        ]
        for name in sorted(set(checked_environment) | set(secret_environment)):
            arguments.extend(("--env", name))
        arguments.extend(("--entrypoint", command.argv[0], profile.image, *command.argv[1:]))
        return PreparedContainerLaunch(
            argv=tuple(arguments),
            base_environment=MappingProxyType(checked_environment),
            secrets=secrets,
            plan_fingerprint=plan.fingerprint,
            profile_digest=profile.digest,
        )

    def _verify_binding(self) -> None:
        try:
            if _file_identity(self._path) != self._identity:
                raise ValueError
        except (OSError, ValueError):
            raise KernelError("sandbox_binding_changed", "容器引擎绑定已经变化") from None

    @staticmethod
    def _network(
        snapshot: NetworkPolicySnapshot,
        expected_gateway_digest: Revision | None,
        egress: ManagedEgressBinding | None,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        mode = snapshot.policy.mode
        if mode == "none":
            if egress is not None:
                raise KernelError("network_policy_unenforceable", "none模式不能绑定出口网关")
            return ("--network", "none"), {}
        if mode == "full":
            if egress is not None:
                raise KernelError("network_policy_unenforceable", "full模式不能伪装选择性出口")
            return ("--network", "bridge"), {}
        if (
            egress is None
            or egress.policy_digest != snapshot.digest
            or egress.gateway_digest != expected_gateway_digest
            or not egress.network_name.startswith("harnessix-internal-")
            or not egress.proxy_url.startswith("http://harnessix-egress:")
        ):
            raise KernelError("network_policy_unenforceable", "选择性网络缺少匹配的内部网关证明")
        # internal_network_attestation和gateway_digest由生命周期管理器通过Docker inspect
        # 绑定到Plan审计；这里要求非空Revision，避免未证明的普通bridge被静默使用。
        return (
            ("--network", egress.network_name),
            {
                "HTTP_PROXY": egress.proxy_url,
                "HTTPS_PROXY": egress.proxy_url,
                "ALL_PROXY": "",
                "NO_PROXY": "",
            },
        )
