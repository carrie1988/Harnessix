"""Windows密钥原生文件句柄：私有ACL、有限读写与不可覆盖重命名。"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harnessix.agent.errors import KernelError
from harnessix.product_config.session_key_codec import MAX_KEY_FILE_BYTES, unavailable
from harnessix.product_config.session_key_windows_security import PrivateKeySecurity
from harnessix.workspace.windows import WindowsWorkspaceRoot, _api_path
from harnessix.workspace.windows_private_security import PrivateWindowsSecurity


class WindowsKeyFiles:
    """借用既有Workspace句柄身份端口，不复刻另一套路径和Reparse解析器。"""

    def __init__(
        self,
        root: WindowsWorkspaceRoot,
        *,
        security_factory: Callable[[Any, Any], PrivateWindowsSecurity] = PrivateKeySecurity,
        shared_reads: bool = False,
    ) -> None:
        self.root = root
        self.kernel = root._kernel32
        self._read_share = 3 if shared_reads else 1
        self._configure()
        advapi = ctypes.__dict__["WinDLL"]("advapi32", use_last_error=True)
        self.security = security_factory(advapi, self.kernel)

    def _configure(self) -> None:
        p, u, b = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
        signatures: dict[str, tuple[list[Any], Any]] = {
            "CreateDirectoryW": ([ctypes.c_wchar_p, p], b),
            "WriteFile": ([p, p, u, ctypes.POINTER(u), p], b),
            "FlushFileBuffers": ([p], b),
            "MoveFileExW": ([ctypes.c_wchar_p, ctypes.c_wchar_p, u], b),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    def create_directory(self, path: Path) -> None:
        if not self.kernel.CreateDirectoryW(
            _api_path(path), ctypes.byref(self.security.attributes)
        ):
            if ctypes.__dict__["get_last_error"]() != 183:
                raise unavailable()

    def open(
        self,
        path: Path,
        *,
        create: int = 3,
        writable: bool = False,
        directory: bool = False,
        exclusive: bool = False,
    ) -> int:
        access = 0x20000 | (0x80 if directory else 0x80000000)
        if writable:
            access |= 0x40000000
        handle = self.kernel.CreateFileW(
            _api_path(path),
            access,
            0 if exclusive else self._read_share,
            ctypes.byref(self.security.attributes) if create != 3 else None,
            create,
            0x00200000 | (0x02000000 if directory else 0),
            None,
        )
        if handle is None or handle == ctypes.c_void_p(-1).value:
            if exclusive and ctypes.__dict__["get_last_error"]() in {32, 33}:
                raise KernelError("publication_key_busy", "Session密钥初始化正在进行")
            raise unavailable()
        owned = int(handle)
        try:
            info = self.root._information(owned)
            if (
                info.attributes & 0x400
                or bool(info.attributes & 0x10) != directory
                or (not directory and info.links != 1)
            ):
                raise unavailable()
            self.security.verify(owned)
            return owned
        except BaseException:
            self.kernel.CloseHandle(owned)
            raise

    def read(self, path: Path) -> bytes:
        return _read_key_file(self, path)

    def write_new(self, path: Path, body: bytes) -> None:
        _write_new_key_file(self, path, body)

    def lock(self, path: Path) -> int:
        return _lock_key_file(self, path)

    def publish(self, temporary: Path, target: Path) -> None:
        # 不设置REPLACE_EXISTING或COPY_ALLOWED；只允许同卷不可覆盖提交。
        if not self.kernel.MoveFileExW(_api_path(temporary), _api_path(target), 0x8):
            raise unavailable()

    def close(self) -> None:
        self.security.close()


def _read_key_file(files: WindowsKeyFiles, path: Path) -> bytes:
    handle = files.open(path)
    try:
        before = files.root._information(handle)
        if before.size_high or before.size_low > MAX_KEY_FILE_BYTES:
            raise unavailable()
        body = ctypes.create_string_buffer(MAX_KEY_FILE_BYTES + 1)
        size = ctypes.c_uint32()
        if not files.kernel.ReadFile(handle, body, len(body), ctypes.byref(size), None):
            raise unavailable()
        after = files.root._information(handle)
        if size.value != before.size_low or files.root._revision_identity(
            before
        ) != files.root._revision_identity(after):
            raise unavailable()
        files.security.verify(handle)
        return body.raw[: size.value]
    finally:
        files.kernel.CloseHandle(handle)


def _write_new_key_file(files: WindowsKeyFiles, path: Path, body: bytes) -> None:
    if not 1 <= len(body) <= MAX_KEY_FILE_BYTES:
        raise unavailable()
    handle = files.open(path, create=1, writable=True, exclusive=True)
    try:
        buffer, offset = ctypes.create_string_buffer(body, len(body)), 0
        while offset < len(body):
            written = ctypes.c_uint32()
            if (
                not files.kernel.WriteFile(
                    handle,
                    ctypes.byref(buffer, offset),
                    len(body) - offset,
                    ctypes.byref(written),
                    None,
                )
                or not 0 < written.value <= len(body) - offset
            ):
                raise unavailable()
            offset += written.value
        if not files.kernel.FlushFileBuffers(handle):
            raise unavailable()
    finally:
        files.kernel.CloseHandle(handle)


def _lock_key_file(files: WindowsKeyFiles, path: Path) -> int:
    import msvcrt

    handle = files.open(path, create=4, writable=True, exclusive=True)
    try:
        # 成功后所有权交给CRT描述符，os.close会同时释放底层原生句柄。
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)  # type: ignore[attr-defined]
    except BaseException:
        files.kernel.CloseHandle(handle)
        raise
    return int(descriptor)
