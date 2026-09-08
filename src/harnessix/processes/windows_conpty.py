from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from typing import Any

from harnessix.agent.errors import KernelError
from harnessix.processes.windows_job import WindowsJobObject

_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_ERROR_INSUFFICIENT_BUFFER = 122
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF
_MIN_CONPTY_BUILD = 17763
_PSEUDOCONSOLE_RESIZE_QUIRK = 0x2


class WindowsTtyInputNormalizer:
    def __init__(self) -> None:
        self._previous_was_cr = False

    def normalize(self, data: bytes) -> bytes:
        output = bytearray()
        for value in data:
            if value == 0x08:
                output.append(0x7F)
            elif value == 0x0A:
                if not self._previous_was_cr:
                    output.append(0x0D)
            else:
                output.append(value)
            self._previous_was_cr = value == 0x0D
        return bytes(output)


class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _OSVERSIONINFOW(ctypes.Structure):
    _fields_ = [
        ("dwOSVersionInfoSize", wintypes.DWORD),
        ("dwMajorVersion", wintypes.DWORD),
        ("dwMinorVersion", wintypes.DWORD),
        ("dwBuildNumber", wintypes.DWORD),
        ("dwPlatformId", wintypes.DWORD),
        ("szCSDVersion", wintypes.WCHAR * 128),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _kernel32() -> Any:
    if os.name != "nt":
        raise KernelError("process_platform_unsupported", "Windows ConPTY不可用")
    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]


def conpty_available() -> bool:
    if os.name != "nt":
        return False
    try:
        ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
        info = _OSVERSIONINFOW()
        info.dwOSVersionInfoSize = ctypes.sizeof(info)
        ntdll.RtlGetVersion.argtypes = (ctypes.POINTER(_OSVERSIONINFOW),)
        ntdll.RtlGetVersion.restype = ctypes.c_long
        kernel32 = _kernel32()
        return (
            ntdll.RtlGetVersion(ctypes.byref(info)) >= 0
            and info.dwBuildNumber >= _MIN_CONPTY_BUILD
            and bool(kernel32.CreatePseudoConsole)
            and bool(kernel32.ResizePseudoConsole)
            and bool(kernel32.ClosePseudoConsole)
        )
    except (AttributeError, OSError):
        return False


def _close_handle(handle: int) -> None:
    if handle:
        kernel32 = _kernel32()
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)


def _pipe() -> tuple[int, int]:
    kernel32 = _kernel32()
    read_handle = wintypes.HANDLE()
    write_handle = wintypes.HANDLE()
    kernel32.CreatePipe.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.CreatePipe.restype = wintypes.BOOL
    if not kernel32.CreatePipe(ctypes.byref(read_handle), ctypes.byref(write_handle), None, 0):
        raise KernelError("process_pty_unavailable", "Windows ConPTY管道创建失败")
    return int(read_handle.value or 0), int(write_handle.value or 0)


def _environment_block(environment: dict[str, str]) -> ctypes.Array[Any]:
    values = "".join(
        f"{name}={value}\0"
        for name, value in sorted(environment.items(), key=lambda x: x[0].casefold())
    )
    return ctypes.create_unicode_buffer(values + "\0")


class WindowsConPtyProcess:
    def __init__(
        self,
        *,
        process_handle: int,
        pid: int,
        job: WindowsJobObject,
        pseudoconsole: int,
        con_input_read: int,
        con_output_write: int,
        input_fd: int,
        output_fd: int,
    ) -> None:
        self._process_handle = process_handle
        self.pid = pid
        self.job = job
        self._pseudoconsole = pseudoconsole
        self._con_input_read = con_input_read
        self._con_output_write = con_output_write
        self.input_fd: int | None = input_fd
        self.output_fd: int | None = output_fd

    def poll(self) -> int | None:
        kernel32 = _kernel32()
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        result = kernel32.WaitForSingleObject(self._process_handle, 0)
        if result == _WAIT_TIMEOUT:
            return None
        if result != _WAIT_OBJECT_0:
            raise KernelError("process_wait_failed", "Windows ConPTY进程等待失败")
        return self._exit_code()

    def wait(self) -> int:
        kernel32 = _kernel32()
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        if kernel32.WaitForSingleObject(self._process_handle, _INFINITE) != _WAIT_OBJECT_0:
            raise KernelError("process_wait_failed", "Windows ConPTY进程等待失败")
        return self._exit_code()

    def _exit_code(self) -> int:
        kernel32 = _kernel32()
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        if not kernel32.GetExitCodeProcess(self._process_handle, ctypes.byref(code)):
            raise KernelError("process_wait_failed", "Windows ConPTY退出码不可用")
        return int(code.value)

    def resize(self, columns: int, rows: int) -> None:
        kernel32 = _kernel32()
        kernel32.ResizePseudoConsole.argtypes = (wintypes.HANDLE, _COORD)
        kernel32.ResizePseudoConsole.restype = ctypes.c_long
        if kernel32.ResizePseudoConsole(self._pseudoconsole, _COORD(columns, rows)) < 0:
            raise KernelError("process_terminal_invalid", "Windows ConPTY尺寸调整失败")

    def close_input(self) -> None:
        if self.input_fd is not None:
            os.close(self.input_fd)
            self.input_fd = None

    def close_pseudoconsole(self) -> None:
        if self._pseudoconsole:
            kernel32 = _kernel32()
            kernel32.ClosePseudoConsole.argtypes = (wintypes.HANDLE,)
            kernel32.ClosePseudoConsole(self._pseudoconsole)
            self._pseudoconsole = 0
            _close_handle(self._con_input_read)
            _close_handle(self._con_output_write)
            self._con_input_read = 0
            self._con_output_write = 0

    def close(self) -> None:
        self.close_input()
        self.close_pseudoconsole()
        if self.output_fd is not None:
            os.close(self.output_fd)
            self.output_fd = None
        _close_handle(self._process_handle)
        self._process_handle = 0


def spawn_conpty(
    argv: tuple[str, ...],
    *,
    cwd: str,
    environment: dict[str, str],
    columns: int,
    rows: int,
) -> WindowsConPtyProcess:
    if not conpty_available():
        raise KernelError("process_pty_unavailable", "当前Windows不支持ConPTY")
    import msvcrt

    input_read = input_write = output_read = output_write = 0
    pseudoconsole = process_handle = thread_handle = 0
    job: WindowsJobObject | None = None
    attributes: ctypes.Array[Any] | None = None
    input_fd = output_fd = -1
    try:
        input_read, input_write = _pipe()
        output_read, output_write = _pipe()
        kernel32 = _kernel32()
        pseudoconsole_handle = wintypes.HANDLE()
        kernel32.CreatePseudoConsole.argtypes = (
            _COORD,
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        kernel32.CreatePseudoConsole.restype = ctypes.c_long
        if (
            kernel32.CreatePseudoConsole(
                _COORD(columns, rows),
                input_read,
                output_write,
                _PSEUDOCONSOLE_RESIZE_QUIRK,
                ctypes.byref(pseudoconsole_handle),
            )
            < 0
        ):
            raise KernelError("process_pty_unavailable", "Windows ConPTY创建失败")
        pseudoconsole = int(pseudoconsole_handle.value or 0)
        job = WindowsJobObject()

        size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        )
        kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel32.UpdateProcThreadAttribute.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel32.DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
        kernel32.DeleteProcThreadAttributeList.restype = None
        ctypes.set_last_error(0)  # type: ignore[attr-defined]
        kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if (
            ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER  # type: ignore[attr-defined]
            or size.value == 0
        ):
            raise KernelError("process_pty_unavailable", "Windows启动属性长度查询失败")
        attributes = ctypes.create_string_buffer(size.value)
        attribute_pointer = ctypes.cast(attributes, ctypes.c_void_p)
        if not kernel32.InitializeProcThreadAttributeList(
            attribute_pointer, 2, 0, ctypes.byref(size)
        ):
            raise KernelError("process_pty_unavailable", "Windows启动属性初始化失败")
        if not kernel32.UpdateProcThreadAttribute(
            attribute_pointer,
            0,
            _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            pseudoconsole,
            ctypes.sizeof(wintypes.HANDLE),
            None,
            None,
        ):
            raise KernelError("process_pty_unavailable", "Windows ConPTY启动属性设置失败")
        job_handles = (wintypes.HANDLE * 1)(job.handle)
        if not kernel32.UpdateProcThreadAttribute(
            attribute_pointer,
            0,
            _PROC_THREAD_ATTRIBUTE_JOB_LIST,
            ctypes.byref(job_handles),
            ctypes.sizeof(job_handles),
            None,
            None,
        ):
            raise KernelError("process_job_assignment_failed", "Windows Job启动属性设置失败")

        startup = _STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.lpAttributeList = attribute_pointer
        process_information = _PROCESS_INFORMATION()
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        environment_block = _environment_block(environment)
        kernel32.CreateProcessW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(_STARTUPINFOW),
            ctypes.POINTER(_PROCESS_INFORMATION),
        )
        kernel32.CreateProcessW.restype = wintypes.BOOL
        if not kernel32.CreateProcessW(
            None,
            command_line,
            None,
            None,
            False,
            _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
            environment_block,
            cwd,
            ctypes.byref(startup.StartupInfo),
            ctypes.byref(process_information),
        ):
            raise KernelError("process_launch_failed", "Windows ConPTY目标启动失败")
        process_handle = int(process_information.hProcess)
        thread_handle = int(process_information.hThread)
        pid = int(process_information.dwProcessId)
        if not job.contains(pid):
            raise KernelError("process_job_assignment_failed", "Windows ConPTY未原子加入Job")
        input_fd = int(msvcrt.open_osfhandle(input_write, os.O_WRONLY))  # type: ignore[attr-defined]
        input_write = 0
        output_fd = int(msvcrt.open_osfhandle(output_read, os.O_RDONLY))  # type: ignore[attr-defined]
        output_read = 0
        _close_handle(thread_handle)
        thread_handle = 0
        return WindowsConPtyProcess(
            process_handle=process_handle,
            pid=pid,
            job=job,
            pseudoconsole=pseudoconsole,
            con_input_read=input_read,
            con_output_write=output_write,
            input_fd=input_fd,
            output_fd=output_fd,
        )
    except BaseException:
        if job is not None:
            try:
                job.terminate()
            except BaseException:
                pass
            job.close()
        if pseudoconsole:
            kernel32 = _kernel32()
            kernel32.ClosePseudoConsole(pseudoconsole)
        for handle in (
            input_read,
            input_write,
            output_read,
            output_write,
            thread_handle,
            process_handle,
        ):
            _close_handle(handle)
        for descriptor in (input_fd, output_fd):
            if descriptor >= 0:
                os.close(descriptor)
        raise
    finally:
        if attributes is not None:
            _kernel32().DeleteProcThreadAttributeList(ctypes.cast(attributes, ctypes.c_void_p))
