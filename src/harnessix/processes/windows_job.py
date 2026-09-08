from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any

from harnessix.agent.errors import KernelError

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SUSPEND_RESUME = 0x0800
STILL_ACTIVE = 259


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32() -> Any:
    if os.name != "nt":
        raise KernelError("process_platform_unsupported", "Windows Job Object不可用")
    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]


class WindowsJobObject:
    def __init__(self) -> None:
        kernel32 = _kernel32()
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise KernelError("process_job_unavailable", "Windows Job Object创建失败")
        self._handle = int(handle)
        information = _EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        if not kernel32.SetInformationJobObject(
            self._handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            self.close()
            raise KernelError("process_job_unavailable", "Windows Job Object策略设置失败")

    @property
    def handle(self) -> int:
        return self._handle

    def assign_suspended(self, pid: int) -> None:
        kernel32 = _kernel32()
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        access = (
            PROCESS_SET_QUOTA
            | PROCESS_TERMINATE
            | PROCESS_QUERY_LIMITED_INFORMATION
            | PROCESS_SUSPEND_RESUME
        )
        process = kernel32.OpenProcess(access, False, pid)
        if not process:
            raise KernelError("process_job_assignment_failed", "Windows目标进程句柄不可用")
        try:
            kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            if not kernel32.AssignProcessToJobObject(self._handle, process):
                raise KernelError("process_job_assignment_failed", "Windows进程未加入Job Object")
            ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
            ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
            ntdll.NtResumeProcess.restype = ctypes.c_long
            if ntdll.NtResumeProcess(process) < 0:
                raise KernelError("process_job_assignment_failed", "Windows挂起进程恢复失败")
        finally:
            self._close_handle(process)

    def contains(self, pid: int) -> bool:
        kernel32 = _kernel32()
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return False
        try:
            result = wintypes.BOOL()
            kernel32.IsProcessInJob.argtypes = (
                wintypes.HANDLE,
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.BOOL),
            )
            kernel32.IsProcessInJob.restype = wintypes.BOOL
            if not kernel32.IsProcessInJob(process, self._handle, ctypes.byref(result)):
                raise KernelError("process_job_query_failed", "Windows Job Object查询失败")
            return bool(result.value)
        finally:
            self._close_handle(process)

    def terminate(self, exit_code: int = 1) -> None:
        kernel32 = _kernel32()
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        if not kernel32.TerminateJobObject(self._handle, exit_code):
            raise KernelError("process_cleanup_failed", "Windows Job Object终止失败")

    def close(self) -> None:
        if getattr(self, "_handle", 0):
            self._close_handle(self._handle)
            self._handle = 0

    @staticmethod
    def _close_handle(handle: int) -> None:
        kernel32 = _kernel32()
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)

    def __enter__(self) -> WindowsJobObject:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
