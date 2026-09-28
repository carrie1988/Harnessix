"""Sandbox与网络隔离：探测并摘要宿主与Container执行能力。"""

from __future__ import annotations

import json
import os
import platform as host_platform
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.sandbox.contracts import SandboxContract
from harnessix.tools.contracts import Revision
from harnessix.workspace.contracts import PlatformKind

ContainerEngineKind = Literal["docker", "podman"]
_CONTAINER_ENGINE_PROBE_TIMEOUT_SECONDS = 15.0


class ContainerEngineProbe(SandboxContract):
    spec_version: Literal["harnessix.container-engine-probe/v1"] = (
        "harnessix.container-engine-probe/v1"
    )
    platform: PlatformKind
    engine: ContainerEngineKind
    client_version: str = Field(min_length=1, max_length=128)
    server_version: str = Field(min_length=1, max_length=128)
    executable_identity: Revision
    available: Literal[True] = True
    rootless: bool
    digest: Revision

    @model_validator(mode="after")
    def self_digest(self) -> Self:
        if self.digest != container_engine_probe_digest(self):
            raise ValueError("容器引擎能力摘要不一致")
        return self


def container_engine_probe_digest(probe: ContainerEngineProbe) -> str:
    return canonical_digest(probe.model_dump(mode="json", exclude={"digest"}, warnings="error"))


def native_platform() -> PlatformKind:
    if os.name == "nt":
        return "windows"
    if os.name == "posix":
        return "posix"
    raise KernelError("sandbox_platform_unsupported", "当前平台不受Sandbox支持")


def executable_identity_digest(path: Path) -> str:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ValueError
    return canonical_digest(
        {
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
            "mode": info.st_mode,
        }
    )


ProbeRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def _run_probe(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
        env={"PATH": os.defpath},
    )


def container_resource_probe_command(
    executable: str | Path, *, engine: ContainerEngineKind
) -> tuple[str, ...]:
    """只查询资源强制所需字段，避免读取可能携带代理配置的完整info。"""

    template = (
        "[{{json .MemoryLimit}},{{json .CPUCfsPeriod}},{{json .CPUCfsQuota}},{{json .PidsLimit}}]"
        if engine == "docker"
        else '{"version":{{json .Host.CgroupVersion}},'
        '"controllers":{{json .Host.CgroupControllers}}}'
    )
    return (str(executable), "info", "--format", template)


def verify_container_resource_support(
    engine: ContainerEngineKind,
    completed: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes],
) -> None:
    """严格验证受信引擎报告；参数被接受不等于内核资源限制可用。"""

    try:
        if type(completed.stdout) not in {str, bytes} or type(completed.stderr) not in {str, bytes}:
            raise ValueError
        output = (
            completed.stdout.encode("utf-8")
            if isinstance(completed.stdout, str)
            else completed.stdout
        )
        errors = (
            completed.stderr.encode("utf-8")
            if isinstance(completed.stderr, str)
            else completed.stderr
        )
        if completed.returncode != 0 or len(output) > 4096 or len(errors) > 16384:
            raise ValueError
        value = json.loads(output.decode("utf-8"))
        if engine == "docker":
            supported = (
                type(value) is list and len(value) == 4 and all(item is True for item in value)
            )
        else:
            supported = _podman_resources_supported(value)
        if not supported:
            raise ValueError
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise KernelError(
            "sandbox_resources_unavailable", "容器引擎无法证明必需资源限制能力"
        ) from None


def _podman_resources_supported(value: object) -> bool:
    """只接受有界v2控制器证明，不把Rootless或宿主CPU数量当作限制能力。"""

    if (
        type(value) is not dict
        or set(value) != {"version", "controllers"}
        or value["version"] != "v2"
    ):
        return False
    controllers = value["controllers"]
    if type(controllers) is not list or len(controllers) > 32:
        return False
    return all(type(item) is str and 1 <= len(item) <= 64 for item in controllers) and {
        "cpu",
        "memory",
        "pids",
    }.issubset(controllers)


def probe_container_engine(
    executable: str | Path,
    *,
    engine: ContainerEngineKind,
    runner: ProbeRunner = _run_probe,
) -> ContainerEngineProbe:
    path = Path(executable)
    try:
        if not path.is_absolute():
            raise ValueError
        path = path.resolve(strict=True)
        identity = executable_identity_digest(path)
        version_command = (
            str(path),
            "version",
            "--format",
            "{{.Client.Version}}|{{.Server.Version}}",
        )
        if engine == "docker":
            security_command = (
                str(path),
                "info",
                "--format",
                "{{json .SecurityOptions}}",
            )
        else:
            security_command = (
                str(path),
                "info",
                "--format",
                "{{.Host.Security.Rootless}}",
            )
        completed = runner(version_command, _CONTAINER_ENGINE_PROBE_TIMEOUT_SECONDS)
        security_completed = runner(security_command, _CONTAINER_ENGINE_PROBE_TIMEOUT_SECONDS)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise KernelError("sandbox_unavailable", "容器引擎探测失败") from None
    if (
        completed.returncode != 0
        or security_completed.returncode != 0
        or len(completed.stdout.encode("utf-8")) > 4096
        or len(security_completed.stdout.encode("utf-8")) > 16384
    ):
        raise KernelError("sandbox_unavailable", "容器引擎服务不可用")
    parts = completed.stdout.strip().split("|", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise KernelError("sandbox_unavailable", "容器引擎版本响应无效")
    client, server = parts
    try:
        if engine == "docker":
            security = json.loads(security_completed.stdout)
            if not isinstance(security, list) or not all(
                isinstance(item, str) for item in security
            ):
                raise ValueError
            rootless = any("rootless" in item.casefold() for item in security)
        else:
            value = security_completed.stdout.strip().casefold()
            if value not in {"true", "false"}:
                raise ValueError
            rootless = value == "true"
    except (json.JSONDecodeError, ValueError, TypeError):
        raise KernelError("sandbox_unavailable", "容器引擎安全能力响应无效") from None
    try:
        resources = runner(
            container_resource_probe_command(path, engine=engine),
            _CONTAINER_ENGINE_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        raise KernelError("sandbox_resources_unavailable", "容器引擎资源能力探测失败") from None
    verify_container_resource_support(engine, resources)
    payload = {
        "spec_version": "harnessix.container-engine-probe/v1",
        "platform": native_platform(),
        "engine": engine,
        "client_version": client,
        "server_version": server,
        "executable_identity": identity,
        "available": True,
        "rootless": rootless,
    }
    return ContainerEngineProbe(
        platform=native_platform(),
        engine=engine,
        client_version=client,
        server_version=server,
        executable_identity=identity,
        available=True,
        rootless=rootless,
        digest=canonical_digest(payload),
    )


class HostSandboxProbe(SandboxContract):
    spec_version: Literal["harnessix.host-sandbox-probe/v1"] = "harnessix.host-sandbox-probe/v1"
    platform: PlatformKind
    guarded_available: Literal[True] = True
    sandboxed_backend: Literal["seatbelt", "bubblewrap"] | None
    sandboxed_available: bool
    backend_identity: Revision | None
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    digest: Revision

    @model_validator(mode="after")
    def self_digest(self) -> Self:
        if self.sandboxed_available != (self.sandboxed_backend is not None):
            raise ValueError("Host Sandbox能力声明矛盾")
        if self.sandboxed_available != (self.backend_identity is not None):
            raise ValueError("Host Sandbox身份声明矛盾")
        expected = canonical_digest(
            self.model_dump(mode="json", exclude={"digest"}, warnings="error")
        )
        if self.digest != expected:
            raise ValueError("Host Sandbox能力摘要不一致")
        return self


def probe_host_sandbox(*, runner: ProbeRunner = _run_probe) -> HostSandboxProbe:
    platform_kind = native_platform()
    backend: Literal["seatbelt", "bubblewrap"] | None = None
    backend_identity: str | None = None
    reason = "native_strong_unavailable"
    if platform_kind == "posix" and host_platform.system() == "Darwin":
        candidate = Path("/usr/bin/sandbox-exec")
        if candidate.is_file() and os.access(candidate, os.X_OK):
            try:
                completed = runner(
                    (
                        str(candidate),
                        "-p",
                        "(version 1)(deny default)(allow process*)",
                        "/usr/bin/true",
                    ),
                    5.0,
                )
                if completed.returncode == 0:
                    backend = "seatbelt"
                    backend_identity = executable_identity_digest(candidate)
                    reason = "seatbelt_preflight_passed"
                else:
                    reason = "seatbelt_preflight_failed"
            except (OSError, ValueError, subprocess.SubprocessError):
                reason = "seatbelt_preflight_failed"
    elif platform_kind == "posix":
        for value in os.environ.get("PATH", os.defpath).split(os.pathsep):
            candidate = Path(value) / "bwrap"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                try:
                    completed = runner(
                        (
                            str(candidate),
                            "--ro-bind",
                            "/",
                            "/",
                            "--dev",
                            "/dev",
                            "--proc",
                            "/proc",
                            "--unshare-net",
                            "--",
                            "/bin/true",
                        ),
                        5.0,
                    )
                    if completed.returncode == 0:
                        backend = "bubblewrap"
                        backend_identity = executable_identity_digest(candidate.resolve())
                        reason = "bubblewrap_preflight_passed"
                    else:
                        reason = "bubblewrap_preflight_failed"
                except (OSError, ValueError, subprocess.SubprocessError):
                    reason = "bubblewrap_preflight_failed"
                break
    payload = {
        "spec_version": "harnessix.host-sandbox-probe/v1",
        "platform": platform_kind,
        "guarded_available": True,
        "sandboxed_backend": backend,
        "sandboxed_available": backend is not None,
        "backend_identity": backend_identity,
        "reason_code": reason,
    }
    return HostSandboxProbe(
        platform=platform_kind,
        guarded_available=True,
        sandboxed_backend=backend,
        sandboxed_available=backend is not None,
        backend_identity=backend_identity,
        reason_code=reason,
        digest=canonical_digest(payload),
    )
