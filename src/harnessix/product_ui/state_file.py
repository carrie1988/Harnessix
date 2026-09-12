"""Client State私有文件、进程锁和原子替换基础设施。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from harnessix.file_lock import acquire_exclusive_file_lock
from harnessix.product_ui.contracts import MAX_CLIENT_STATE_BYTES, ClientStateV1
from harnessix.product_ui.errors import ProductUIError


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _private_owner(info: os.stat_result, mode: int) -> bool:
    return os.name != "posix" or (info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == mode)


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("重复字段")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("非有限JSON数值")


def prepare_private_directory(root: Path) -> None:
    try:
        if root.exists() or _is_link_or_junction(root):
            info = root.lstat()
            if _is_link_or_junction(root) or not stat.S_ISDIR(info.st_mode):
                raise OSError
        else:
            root.mkdir(mode=0o700, parents=True)
            info = root.lstat()
        if not _private_owner(info, 0o700):
            raise OSError
    except OSError:
        raise ProductUIError("client_state_permissions", "客户端状态目录权限或身份不安全") from None


def open_state_lock(path: Path) -> int:
    """安全打开并非阻塞获取生命周期锁，返回由调用方持有的描述符。"""

    flags = os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        created = False
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        linked = path.lstat()
        same_regular_file = (
            stat.S_ISREG(opened.st_mode)
            and stat.S_ISREG(linked.st_mode)
            and opened.st_dev == linked.st_dev
            and opened.st_ino == linked.st_ino
            and opened.st_nlink == 1
        )
        if not same_regular_file or (os.name == "posix" and opened.st_uid != os.getuid()):
            raise OSError
        if created and os.name == "posix":
            os.fchmod(descriptor, 0o600)
            opened = os.fstat(descriptor)
        if not _private_owner(opened, 0o600):
            raise OSError
        if opened.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        acquire_exclusive_file_lock(descriptor)
        return descriptor
    except BlockingIOError:
        if descriptor is not None:
            os.close(descriptor)
        raise ProductUIError(
            "client_state_busy", "客户端状态正由另一实例使用", retryable=True
        ) from None
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise ProductUIError("client_state_permissions", "客户端状态锁权限或身份不安全") from None


def _open_state_file(path: Path) -> tuple[int, int]:
    if _is_link_or_junction(path):
        raise ProductUIError("client_state_permissions", "客户端状态文件权限或身份不安全")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    opened = os.fstat(descriptor)
    linked = path.lstat()
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or opened.st_dev != linked.st_dev
        or opened.st_ino != linked.st_ino
        or opened.st_nlink != 1
        or not _private_owner(opened, 0o600)
    ):
        os.close(descriptor)
        raise ProductUIError("client_state_permissions", "客户端状态文件权限或身份不安全")
    if not 1 <= opened.st_size <= MAX_CLIENT_STATE_BYTES:
        os.close(descriptor)
        raise ValueError
    return descriptor, opened.st_size


def _read_exact(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_CLIENT_STATE_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    body = b"".join(chunks)
    if len(body) != expected_size:
        raise ValueError
    return body


def _decode_state(body: bytes, workspace_fingerprint: str) -> ClientStateV1:
    document = json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(document, dict):
        raise ValueError
    if document.get("spec_version") != "harnessix.client-state/v1":
        raise ProductUIError("client_state_version", "客户端状态版本不受支持")
    state = ClientStateV1.model_validate_json(
        json.dumps(document, ensure_ascii=False, allow_nan=False), strict=True
    )
    if state.workspace_fingerprint != workspace_fingerprint:
        raise ProductUIError("client_state_workspace_mismatch", "客户端状态不属于当前Workspace")
    return state


def read_state_file(path: Path, workspace_fingerprint: str) -> ClientStateV1:
    descriptor: int | None = None
    try:
        descriptor, expected_size = _open_state_file(path)
        return _decode_state(_read_exact(descriptor, expected_size), workspace_fingerprint)
    except ProductUIError:
        raise
    except FileNotFoundError:
        raise ProductUIError("client_state_corrupt", "客户端状态文件缺失") from None
    except PermissionError:
        raise ProductUIError("client_state_permissions", "客户端状态文件权限或身份不安全") from None
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError):
        raise ProductUIError("client_state_corrupt", "客户端状态文件损坏") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _sync_directory(root: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_state_file(root: Path, path: Path, state: ClientStateV1) -> None:
    body = (state.model_dump_json(indent=2, warnings="error") + "\n").encode("utf-8")
    if len(body) > MAX_CLIENT_STATE_BYTES:
        raise ProductUIError("client_state_limit", "客户端状态超过大小上限")
    temporary = root / f".client-state.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= (
            getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(temporary, flags, 0o600)
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        _sync_directory(root)
    except OSError:
        raise ProductUIError("client_state_write_failed", "客户端状态写入失败") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except OSError:
            pass
