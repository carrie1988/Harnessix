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
        completed = runner(version_command, 5.0)
        security_completed = runner(security_command, 5.0)
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
