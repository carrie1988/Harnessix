"""Windows用户DPAPI与原生文件身份生命周期；旧库无Key不生成替代身份。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.file_lock import acquire_exclusive_file_lock
from harnessix.product_config.session_key_codec import (
    WINDOWS_MAGIC,
    create_payload,
    decode_payload,
    unavailable,
)
from harnessix.product_config.session_key_dpapi import transform
from harnessix.product_config.session_key_windows_files import WindowsKeyFiles
from harnessix.workspace.windows import WindowsWorkspaceRoot


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _decode(body: bytes) -> bytes:
    if (
        not body.startswith(WINDOWS_MAGIC)
        or len(body) < 10
        or int.from_bytes(body[5:9], "little") != len(body) - 9
    ):
        raise unavailable()
    payload = transform(body[9:], unprotect=True)
    decode_payload(payload).close()
    return payload


def _load_locked(files: WindowsKeyFiles, root: Path, fault: Callable[[str], None]) -> bytes:
    folder = root / "session-auth"
    key, pending = folder / "key.v1", folder / "key.v1.pending"
    descriptor = files.lock(folder / ".lock")
    try:
        acquire_exclusive_file_lock(descriptor)
        if not _exists(key):
            if any(
                _exists(root / name)
                for name in ("sessions.db", "sessions.db-wal", "sessions.db-shm")
            ):
                raise unavailable()
            if _exists(pending):
                _decode(files.read(pending))
            else:
                sealed = transform(create_payload(), unprotect=False)
                files.write_new(pending, WINDOWS_MAGIC + len(sealed).to_bytes(4, "little") + sealed)
            fault("key.after_pending")
            files.publish(pending, key)
            fault("key.after_publish")
        elif _exists(pending):
            # 成功重命名后原候选应不存在，陌生并存文件不自动删除。
            raise unavailable()
        payload = _decode(files.read(key))
        fault("key.before_return")
        return payload
    finally:
        os.close(descriptor)


def load_windows_key(path: Path, fault: Callable[[str], None]) -> bytes:
    """复用逐段句柄链；私有子目录不接受继承、其他Owner或扩张ACL。"""
    if os.name != "nt" or str(path).startswith("\\\\"):
        raise unavailable()
    root, files = None, None
    handles: list[int] = []
    private: int | None = None
    try:
        root = WindowsWorkspaceRoot(path)
        files = WindowsKeyFiles(root)
        folder = path / "session-auth"
        files.create_directory(folder)
        handles, _ = root._open_chain("session-auth", data=False)
        private = files.open(folder, directory=True)
        payload = _load_locked(files, path, fault)
        files.security.verify(private)
        return payload
    except BlockingIOError:
        raise KernelError("publication_key_busy", "Session密钥初始化正在进行") from None
    except KernelError as error:
        if error.code in {"publication_key_unavailable", "publication_key_busy"}:
            raise
        raise unavailable() from None
    except OSError:
        raise unavailable() from None
    finally:
        if files is not None:
            if private is not None:
                files.kernel.CloseHandle(private)
            files.close()
        if root is not None:
            root._close_all(handles)
            root.close()
