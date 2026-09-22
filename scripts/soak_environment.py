"""采集Soak基线所需的低敏平台与硬件事实。"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Literal

from pydantic import Field, StrictInt

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel


class SoakEnvironment(ContractModel):
    """不包含主机名、账户、路径或进程身份的环境档位。"""

    platform: Literal["linux", "macos", "windows"]
    python_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    cpu_count: StrictInt = Field(gt=0)
    physical_memory_bytes: StrictInt = Field(gt=0)
    hardware_class: str = Field(pattern=r"^c[0-9]+-m[0-9]+$")


def _windows_physical_memory() -> int:
    """按MEMORYSTATUSEX ABI读取物理内存总字节。"""

    import ctypes
    from ctypes import wintypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(MemoryStatusEx),)
        kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError
        return int(status.ullTotalPhys)
    except (AttributeError, OSError, TypeError, ValueError):
        raise KernelError("soak_environment_unavailable", "Windows物理内存信息不可用") from None


def _physical_memory_bytes() -> int:
    if sys.platform == "linux":
        try:
            return int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError):
            raise KernelError("soak_environment_unavailable", "Linux物理内存信息不可用") from None
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ("/usr/sbin/sysctl", "-n", "hw.memsize"),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            return int(result.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            raise KernelError("soak_environment_unavailable", "macOS物理内存信息不可用") from None
    if sys.platform == "win32":
        return _windows_physical_memory()
    raise KernelError("soak_environment_unavailable", "当前平台不支持环境采集")


def read_environment() -> SoakEnvironment:
    """失败关闭地获取平台、Python、核数和物理内存档位。"""

    platform: Literal["linux", "macos", "windows"]
    if sys.platform == "linux":
        platform = "linux"
    elif sys.platform == "darwin":
        platform = "macos"
    elif sys.platform == "win32":
        platform = "windows"
    else:
        raise KernelError("soak_environment_unavailable", "当前平台不支持环境采集")
    cpus = os.cpu_count()
    physical_bytes = _physical_memory_bytes()
    if cpus is None or cpus <= 0 or physical_bytes <= 0:
        raise KernelError("soak_environment_unavailable", "环境容量读数无效")
    gib_rounded_up = (physical_bytes + (1 << 30) - 1) // (1 << 30)
    return SoakEnvironment(
        platform=platform,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        cpu_count=cpus,
        physical_memory_bytes=physical_bytes,
        hardware_class=f"c{cpus}-m{gib_rounded_up}",
    )
