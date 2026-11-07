"""原生沙箱Profile的证明与启动准备（ADR 0111）：探测后端、推导策略、绑定执行计划、生成argv。

与外部边界（ADR 0121）共用原生命令的工具身份与宿主执行器；本模块只负责沙箱后端特有的部分。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import (
    ExecutionCapabilityEvidenceV2,
    NetworkMode,
    SandboxBindingV2,
    canonical_digest,
)
from harnessix.execution.planner import build_capability_evidence_v2
from harnessix.processes.supervision_contracts import ProcessCapabilityProbe
from harnessix.sandbox.bubblewrap import (
    bubblewrap_argv,
    denied_path_masks,
    hidden_runtime_roots,
    prepare_private_directories,
    workspace_env_files,
)
from harnessix.sandbox.capabilities import ProbeRunner, executable_identity_digest
from harnessix.sandbox.native_contracts import HostSandboxProbeV2, NativeSandboxPolicy
from harnessix.sandbox.native_policy import (
    NativeSandboxFacts,
    build_native_sandbox_policy,
    environment_proxy_urls,
    loopback_proxy_ports,
    macos_system_proxy_urls,
    resolved_directory,
    sandbox_environment,
    workspace_git_directories,
)
from harnessix.sandbox.native_probe import native_sandbox_platform, probe_native_sandbox
from harnessix.sandbox.seatbelt import seatbelt_argv

from .action_contracts import ProductNativeProcessProfile

# 实际生效位置可经环境变量改到默认目录之外，必须一并拒读（ADR 0111第2.3节）。
_CONFIGURED_STATE_VARIABLES = ("HARNESSIX_PRODUCT_CONFIG", "HARNESSIX_PRODUCT_STATE_DIRECTORY")
_GIT_CANDIDATES = (Path("/usr/bin/git"), Path("/usr/local/bin/git"), Path("/opt/homebrew/bin/git"))
SandboxProbe = Callable[[], HostSandboxProbeV2]
_SCUTIL = "/usr/sbin/scutil"
_PREFLIGHT_TIMEOUT_SECONDS = 10.0


def _run_quietly(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
        env={"PATH": "/usr/bin:/bin:/usr/sbin", "LANG": "C"},
    )


@dataclass(frozen=True, slots=True)
class NativeSandboxSite:
    """沙箱所需的宿主位置；state_root未知（如无状态诊断）时只拒读默认与环境变量指定的位置。"""

    workspace_root: Path
    state_root: Path | None = None
    protected_paths: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeSandboxBinding:
    """一次启动内固定的沙箱事实：探测结果、策略与启动所需的宿主参数。"""

    probe: HostSandboxProbeV2
    policy: NativeSandboxPolicy
    hidden_roots: tuple[str, ...]
    git_executable: Path | None


@dataclass(frozen=True, slots=True)
class AttestedNativeSandbox:
    capabilities: ExecutionCapabilityEvidenceV2
    sandbox: SandboxBindingV2
    environment: Mapping[str, str]
    binding: NativeSandboxBinding


def workspace_sandbox_root(home: Path, workspace: Path, environ: Mapping[str, str]) -> Path:
    """按工作区隔离、跨会话保留的私有目录根；位于用户缓存目录，不在Harnessix状态目录内。"""

    digest = hashlib.sha256(
        b"harnessix-native-sandbox-workspace/v1\0" + os.fsencode(workspace)
    ).hexdigest()[:32]
    if native_sandbox_platform() == "macos":
        cache = home / "Library/Caches"
    else:
        configured = environ.get("XDG_CACHE_HOME", "")
        cache = Path(configured) if configured.startswith("/") else home / ".cache"
    return cache / "harnessix" / "sandbox" / digest


def attest_native_sandbox(
    profile: ProductNativeProcessProfile,
    owner_capability: ProcessCapabilityProbe,
    *,
    environ: Mapping[str, str],
    site: NativeSandboxSite,
    secret_names: Iterable[str] = (),
    probe: SandboxProbe = probe_native_sandbox,
    runner: ProbeRunner = _run_quietly,
) -> tuple[str, AttestedNativeSandbox | None]:
    """探测后端并推导策略；任何一步不成立都返回原因码，不降级为其他执行方式。"""

    result = probe()
    if result.backend != profile.backend:
        return "native_backend_platform_mismatch", None
    if not result.available or result.backend is None:
        return result.reason_code, None
    seatbelt = result.backend == "seatbelt"
    try:
        home = resolved_directory(environ.get("HOME") or Path.home())
        workspace = resolved_directory(site.workspace_root)
        root = workspace_sandbox_root(home, workspace, environ)
        facts = NativeSandboxFacts(
            backend=result.backend,
            home=home,
            workspace=workspace,
            workspace_mode=profile.workspace_mode,
            private_tmp=root / "tmp",
            private_cache=root / "cache",
            git_directories=workspace_git_directories(workspace),
            write_roots=tuple(resolved_directory(item) for item in profile.write_roots),
            protected_paths=_protected_paths(site, environ),
            allow_workspace_env_files=profile.allow_workspace_env_files,
            blocked_loopback_ports=configured_proxy_ports(environ, runner) if seatbelt else (),
        )
        policy = build_native_sandbox_policy(facts)
    except KernelError as error:
        return error.code, None
    if seatbelt and not _seatbelt_policy_runs(policy, result, runner):
        return "seatbelt_preflight_failed", None
    # Seatbelt放行宿主回环，Harnessix不能证明断网，如实记为full（ADR 0111第2.4节）。
    network: NetworkMode = "full" if seatbelt else "none"
    environment = sandbox_environment(
        environ,
        private_tmp=facts.private_tmp,
        private_cache=facts.private_cache,
        presets=profile.environment,
        secret_names=secret_names,
    )
    capabilities = build_capability_evidence_v2(
        platform="posix",
        provider=result.backend,
        provider_version=result.backend_version or "unknown",
        sandbox_levels=("host_sandboxed",),
        network_modes=(network,),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
        provider_evidence_digest=canonical_digest(
            {"probe_sha256": result.digest, "owner_capability_sha256": owner_capability.digest}
        ),
    )
    sandbox = SandboxBindingV2(
        level="host_sandboxed",
        backend=result.backend,
        backend_version=result.backend_version or "unknown",
        network=network,
        capability_digest=capabilities.evidence_digest,
        profile_digest=canonical_digest(
            {
                "spec_version": "harnessix.native-sandbox-profile/v1",
                "profile_sha256": profile.profile_sha256,
                "policy_sha256": policy.digest,
                "probe_sha256": result.digest,
                "owner_capability_sha256": owner_capability.digest,
                "environment": dict(environment),
            }
        ),
    )
    binding = NativeSandboxBinding(
        probe=result,
        policy=policy,
        hidden_roots=hidden_runtime_roots(),
        git_executable=next((path for path in _GIT_CANDIDATES if path.is_file()), None),
    )
    return "verified", AttestedNativeSandbox(capabilities, sandbox, environment, binding)


def prepare_sandboxed_argv(
    binding: NativeSandboxBinding, command: tuple[str, ...]
) -> tuple[str, ...]:
    """启动前复核后端身份、清空私有TMPDIR并生成argv；bubblewrap另需查找并遮盖`.env`。"""

    probe = binding.probe
    if probe.executable is None or probe.executable_identity is None:
        raise KernelError("sandbox_backend_changed", "沙箱后端身份缺失")
    try:
        identity = executable_identity_digest(Path(probe.executable))
    except (OSError, ValueError):
        raise KernelError("sandbox_backend_changed", "沙箱后端可执行文件已不可用") from None
    if identity != probe.executable_identity:
        raise KernelError("sandbox_backend_changed", "沙箱后端可执行文件已变化")
    policy = binding.policy
    prepare_private_directories(policy)
    if policy.backend == "seatbelt":
        return seatbelt_argv(policy, command=command, executable=probe.executable)
    env_files = (
        workspace_env_files(Path(policy.workspace), git=binding.git_executable)
        if policy.deny_workspace_env_files
        else ()
    )
    return bubblewrap_argv(
        policy,
        executable=probe.executable,
        command=command,
        masks=denied_path_masks(policy),
        env_files=env_files,
        hidden_roots=binding.hidden_roots,
    )


def configured_proxy_ports(environ: Mapping[str, str], runner: ProbeRunner) -> tuple[int, ...]:
    """App Server环境与macOS系统设置中指向本机回环的代理端口（ADR 0111第2.4节）。"""

    urls = list(environment_proxy_urls(environ))
    try:
        completed = runner((_SCUTIL, "--proxy"), _PREFLIGHT_TIMEOUT_SECONDS)
    except (OSError, ValueError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        urls.extend(macos_system_proxy_urls(completed.stdout))
    return loopback_proxy_ports(urls)


def _seatbelt_policy_runs(
    policy: NativeSandboxPolicy, probe: HostSandboxProbeV2, runner: ProbeRunner
) -> bool:
    """用将要执行的同一份策略运行/usr/bin/true；系统拒绝策略时整体省略工具，而不是每条命令失败。"""

    try:
        argv = seatbelt_argv(policy, command=("/usr/bin/true",), executable=probe.executable or "")
        completed = runner(argv, _PREFLIGHT_TIMEOUT_SECONDS)
    except (KernelError, OSError, ValueError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _protected_paths(site: NativeSandboxSite, environ: Mapping[str, str]) -> tuple[Path, ...]:
    paths = [*site.protected_paths]
    if site.state_root is not None:
        paths.append(site.state_root)
    paths.extend(
        Path(environ[name])
        for name in _CONFIGURED_STATE_VARIABLES
        if environ.get(name, "").startswith("/")
    )
    resolved = []
    for path in paths:
        try:
            resolved.append(path.resolve())
        except (OSError, RuntimeError):
            resolved.append(path)
    return tuple(resolved)
