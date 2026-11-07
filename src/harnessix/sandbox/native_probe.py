"""原生进程沙箱（ADR 0111）：探测本机后端是否可用并给出原因码。

可执行文件只查固定路径，不按PATH查找；预检真实运行一次与正式执行同类的最小沙箱。
"""

from __future__ import annotations

import ctypes
import os
import platform as host_platform
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from harnessix.sandbox.capabilities import ProbeRunner, executable_identity_digest
from harnessix.sandbox.native_contracts import (
    NATIVE_GUARANTEES,
    HostSandboxProbeV2,
    NativeSandboxBackend,
    NativeSandboxPlatform,
    host_sandbox_probe_v2_digest,
    native_backend_for_platform,
)

SEATBELT_EXECUTABLE = Path("/usr/bin/sandbox-exec")
BUBBLEWRAP_CANDIDATES = (Path("/usr/bin/bwrap"), Path("/usr/local/bin/bwrap"))
_PREFLIGHT_TIMEOUT_SECONDS = 10.0
_SEATBELT_PREFLIGHT_POLICY = (
    "(version 1)(deny default)(allow process-exec)(allow process-fork)(allow file-read*)"
)
_BUBBLEWRAP_PREFLIGHT = (
    "--ro-bind",
    "/",
    "/",
    "--dev",
    "/dev",
    "--proc",
    "/proc",
    "--unshare-all",
    "--die-with-parent",
    "--new-session",
    "--cap-drop",
    "ALL",
    "--",
    "/bin/true",
)
# bubblewrap在不能创建命名空间时的典型报错（内核禁用、AppArmor限制或容器seccomp）。
_NAMESPACE_DENIED = re.compile(
    r"namespace|uid map|setting up uid|operation not permitted|permission denied",
    re.IGNORECASE,
)
NestedSandboxCheck = Callable[[], bool]


def native_sandbox_platform() -> NativeSandboxPlatform:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def seatbelt_already_applied() -> bool:
    """当前进程是否已在Seatbelt沙箱中；Seatbelt不能嵌套，嵌套时sandbox-exec必然失败。"""

    try:
        library = ctypes.CDLL("/usr/lib/system/libsystem_sandbox.dylib")
        check = library.sandbox_check
        check.restype = ctypes.c_int
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        return bool(check(os.getpid(), None, 0))
    except (OSError, AttributeError):
        return False


def _run_probe(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
        env={"PATH": "/usr/bin:/bin", "LANG": "C"},
    )


def probe_native_sandbox(
    *,
    platform: NativeSandboxPlatform | None = None,
    runner: ProbeRunner = _run_probe,
    nested: NestedSandboxCheck = seatbelt_already_applied,
    bubblewrap_candidates: Sequence[Path] = BUBBLEWRAP_CANDIDATES,
    seatbelt_executable: Path = SEATBELT_EXECUTABLE,
) -> HostSandboxProbeV2:
    """探测当前平台唯一对应的后端；任何异常都折算为不可用原因码。"""

    kind = native_sandbox_platform() if platform is None else platform
    backend = native_backend_for_platform(kind)
    if backend == "seatbelt":
        return _probe_seatbelt(seatbelt_executable, runner, nested)
    if backend == "bubblewrap":
        return _probe_bubblewrap(bubblewrap_candidates, runner)
    return _result(kind, None, reason="native_backend_platform_mismatch")


def _probe_seatbelt(
    executable: Path, runner: ProbeRunner, nested: NestedSandboxCheck
) -> HostSandboxProbeV2:
    if not (executable.is_file() and os.access(executable, os.X_OK)):
        return _result("macos", "seatbelt", reason="seatbelt_not_installed")
    try:
        identity = executable_identity_digest(executable)
    except (OSError, ValueError):
        return _result("macos", "seatbelt", reason="seatbelt_not_installed")
    version = host_platform.mac_ver()[0] or "unknown"
    identified = {"executable": str(executable), "identity": identity, "version": version}
    if nested():
        return _result("macos", "seatbelt", reason="seatbelt_nested_unavailable", **identified)
    try:
        completed = runner(
            (str(executable), "-p", _SEATBELT_PREFLIGHT_POLICY, "/usr/bin/true"),
            _PREFLIGHT_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return _result("macos", "seatbelt", reason="seatbelt_preflight_failed", **identified)
    if completed.returncode != 0:
        return _result("macos", "seatbelt", reason="seatbelt_preflight_failed", **identified)
    return _result("macos", "seatbelt", reason="verified", **identified)


def _probe_bubblewrap(candidates: Sequence[Path], runner: ProbeRunner) -> HostSandboxProbeV2:
    executable = next(
        (path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None
    )
    if executable is None:
        return _result("linux", "bubblewrap", reason="bubblewrap_not_installed")
    try:
        resolved = executable.resolve(strict=True)
        identity = executable_identity_digest(resolved)
        version = runner((str(resolved), "--version"), _PREFLIGHT_TIMEOUT_SECONDS)
    except (OSError, ValueError, subprocess.SubprocessError):
        return _result("linux", "bubblewrap", reason="bubblewrap_not_installed")
    text = version.stdout.strip() if version.returncode == 0 else ""
    if not text or len(text) > 128 or "\n" in text:
        return _result("linux", "bubblewrap", reason="bubblewrap_preflight_failed")
    identified = {"executable": str(resolved), "identity": identity, "version": text}
    try:
        completed = runner((str(resolved), *_BUBBLEWRAP_PREFLIGHT), _PREFLIGHT_TIMEOUT_SECONDS)
    except (OSError, ValueError, subprocess.SubprocessError):
        return _result("linux", "bubblewrap", reason="bubblewrap_preflight_failed", **identified)
    if completed.returncode == 0:
        return _result("linux", "bubblewrap", reason="verified", **identified)
    reason = (
        "bubblewrap_namespaces_denied"
        if _NAMESPACE_DENIED.search(completed.stderr or "")
        else "bubblewrap_preflight_failed"
    )
    return _result("linux", "bubblewrap", reason=reason, **identified)


def _result(
    platform: NativeSandboxPlatform,
    backend: NativeSandboxBackend | None,
    *,
    reason: str,
    executable: str | None = None,
    identity: str | None = None,
    version: str | None = None,
) -> HostSandboxProbeV2:
    available = reason == "verified"
    candidate = HostSandboxProbeV2.model_construct(
        platform=platform,
        backend=backend,
        available=available,
        executable=executable,
        executable_identity=identity,
        backend_version=version,
        reason_code=reason,
        guarantees=NATIVE_GUARANTEES[backend] if available and backend is not None else (),
        digest="0" * 64,
    )
    return HostSandboxProbeV2(
        **candidate.model_dump(exclude={"digest"}),
        digest=host_sandbox_probe_v2_digest(candidate),
    )
