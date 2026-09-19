"""按平台证明Process Owner实现与解释器身份。"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.processes.supervision_contracts import ProcessCapabilityProbe
from harnessix.processes.supervision_planner import build_process_capability
from harnessix.processes.windows_conpty import conpty_available


def posix_process_implementation_digest() -> str:
    """绑定POSIX Owner源码、解释器对象和文件身份。"""

    root = Path(__file__).parent
    paths = tuple(
        root / name
        for name in (
            "owner_protocol.py",
            "owner_output.py",
            "owner_receipt.py",
            "posix_owner.py",
            "supervision_contracts.py",
            "supervision_planner.py",
            "supervisor.py",
        )
    )
    try:
        executable = Path(sys.executable).resolve(strict=True)
        executable_info = executable.stat()
        payload = {
            "implementation": "harnessix.posix-process-owner/v1",
            "python": {
                "path": str(executable),
                "device": executable_info.st_dev,
                "inode": executable_info.st_ino,
                "size": executable_info.st_size,
                "mtime_ns": executable_info.st_mtime_ns,
            },
            "modules": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
        }
    except OSError:
        raise KernelError("process_capability_unavailable", "POSIX Process实现不可证明") from None
    return canonical_digest(payload)


def probe_posix_process_capability() -> ProcessCapabilityProbe:
    """探测当前宿主是否具备受支持的POSIX Process Owner。"""

    if os.name != "posix":
        raise KernelError("process_platform_unsupported", "POSIX Process能力不可用")
    return build_process_capability(
        platform="posix",
        supports_pty=True,
        implementation_digest=posix_process_implementation_digest(),
    )


def windows_process_implementation_digest() -> str:
    """绑定Windows Owner源码与三个必要可执行文件。"""

    if os.name != "nt":
        raise KernelError("process_platform_unsupported", "Windows Process能力不可用")
    root = Path(__file__).parent
    paths = tuple(
        root / name
        for name in (
            "owner_output.py",
            "owner_protocol.py",
            "owner_receipt.py",
            "supervision_contracts.py",
            "supervision_planner.py",
            "supervisor.py",
            "windows_job.py",
            "windows_conpty.py",
            "windows_owner.py",
        )
    )
    executables = (
        Path(sys.executable),
        Path(shutil.which("cmd.exe") or ""),
        Path(shutil.which("powershell.exe") or ""),
    )
    try:
        if any(not path.is_absolute() for path in executables):
            raise OSError
        payload = {
            "implementation": "harnessix.windows-process-owner/v1",
            "executables": {
                str(path.resolve(strict=True)): {
                    "size": path.stat().st_size,
                    "mtime_ns": path.stat().st_mtime_ns,
                }
                for path in executables
            },
            "modules": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
        }
    except OSError:
        raise KernelError("process_capability_unavailable", "Windows Process实现不可证明") from None
    return canonical_digest(payload)


def probe_windows_process_capability() -> ProcessCapabilityProbe:
    """探测当前宿主是否具备受支持的Windows Job/ConPTY Owner。"""

    return build_process_capability(
        platform="windows",
        supports_pty=conpty_available(),
        implementation_digest=windows_process_implementation_digest(),
    )
