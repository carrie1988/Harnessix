"""读取当前进程峰值RSS，并保留可复核的平台单位依据。"""

from __future__ import annotations

import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel


class RssObservation(ContractModel):
    """原始读数、单位、归一化结果与单位验证状态。"""

    source: Literal["getrusage", "proc_status", "GetProcessMemoryInfo"]
    raw_value: StrictInt = Field(gt=0)
    raw_unit: Literal["bytes", "KiB"]
    normalization: Literal["identity", "kib_times_1024"]
    rss_bytes: StrictInt = Field(gt=0)
    unit_verified: Literal[True]


def _unit_from_probe(raw: int, ps_kib: int) -> Literal["bytes", "KiB"]:
    """在受控子进程中用ps当前RSS区分相差1024倍的单位。"""

    if raw <= 0 or ps_kib <= 0:
        raise KernelError("soak_rss_unit_unknown", "RSS单位探针结果无效")
    ratio = raw / ps_kib
    if 256 <= ratio <= 4096:
        return "bytes"
    if 0.25 <= ratio <= 4:
        return "KiB"
    raise KernelError("soak_rss_unit_unknown", "RSS单位探针结果不明确")


@lru_cache(maxsize=1)
def _macos_unit() -> Literal["bytes", "KiB"]:
    """子进程先分配固定内存，再比较ru_maxrss与ps的KiB读数。"""

    probe = """
import os, resource, subprocess
allocation = bytearray(64 * 1024 * 1024)
allocation[0] = 1
raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
ps_kib = int(subprocess.check_output(
    ['/bin/ps', '-o', 'rss=', '-p', str(os.getpid())], text=True, timeout=5
).strip())
print(f'{raw},{ps_kib}')
"""
    try:
        result = subprocess.run(
            (sys.executable, "-I", "-c", probe),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        raw_text, ps_text = result.stdout.strip().split(",")
        return _unit_from_probe(int(raw_text), int(ps_text))
    except (OSError, ValueError, subprocess.SubprocessError):
        raise KernelError("soak_rss_unit_unknown", "RSS单位探针失败") from None


def _windows_peak_working_set() -> int:
    """按PROCESS_MEMORY_COUNTERS ABI读取当前进程峰值工作集字节。"""

    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        )
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        success = psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        if not success or counters.WorkingSetSize > counters.PeakWorkingSetSize:
            raise OSError
        return int(counters.PeakWorkingSetSize)
    except (AttributeError, OSError, TypeError, ValueError):
        raise KernelError("soak_rss_unavailable", "Windows进程内存读数不可用") from None


def _linux_peak_kib(status: str) -> int:
    """只接受/proc状态中唯一、正值且明确标注kB的VmHWM。"""

    values = [line.split() for line in status.splitlines() if line.startswith("VmHWM:")]
    if len(values) != 1 or len(values[0]) != 3 or values[0][2] != "kB":
        raise KernelError("soak_rss_unit_unknown", "Linux RSS单位核对失败")
    try:
        raw = int(values[0][1])
    except ValueError:
        raise KernelError("soak_rss_unit_unknown", "Linux RSS单位核对失败") from None
    if raw <= 0:
        raise KernelError("soak_rss_unit_unknown", "Linux RSS单位核对失败")
    return raw


def read_peak_rss() -> RssObservation:
    """读取进程峰值RSS；未知平台、单位或零读数一律失败关闭。"""

    if sys.platform == "win32":
        raw = _windows_peak_working_set()
        source: Literal["getrusage", "GetProcessMemoryInfo"] = "GetProcessMemoryInfo"
        raw_unit: Literal["bytes", "KiB"] = "bytes"
    elif sys.platform == "linux":
        try:
            raw = _linux_peak_kib(Path("/proc/self/status").read_text(encoding="ascii"))
        except (OSError, UnicodeError):
            raise KernelError("soak_rss_unavailable", "Linux RSS读数不可用") from None
        source = "proc_status"
        raw_unit = "KiB"
    elif sys.platform == "darwin":
        import resource

        try:
            raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        except (OSError, ValueError):
            raise KernelError("soak_rss_unavailable", "RSS读数不可用") from None
        source = "getrusage"
        raw_unit = _macos_unit()
    else:
        raise KernelError("soak_rss_unavailable", "当前平台不支持RSS采集")
    if raw <= 0:
        raise KernelError("soak_rss_unavailable", "RSS读数无效")
    normalization: Literal["identity", "kib_times_1024"] = (
        "identity" if raw_unit == "bytes" else "kib_times_1024"
    )
    return RssObservation(
        source=source,
        raw_value=raw,
        raw_unit=raw_unit,
        normalization=normalization,
        rss_bytes=raw if raw_unit == "bytes" else raw * 1024,
        unit_verified=True,
    )
