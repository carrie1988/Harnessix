"""Git 私有材料路径、配置和完整 RO 快照；不负责产品批准或启动 Git。

common/objects/fanout/info/pack 拒绝链接，alternates 和外部配置失败关闭。
持有目录和复验身份不是任意同 UID 外部改写的 OS 不可变封印。
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import time
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO, NoReturn

from harnessix.delivery.git_material_input_contracts import (
    MAX_CONTROL_FILE_BYTES,
    MAX_MATERIAL_BYTES,
    GitControlFileBinding,
    GitMaterialInput,
    GitMaterialInputError,
    path_identity,
)
from harnessix.delivery.git_material_native_windows import _Windows

_READ = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class _Resources(ExitStack):
    """只缓存本次已持有的目录，避免每个 fanout 重复占用完整父链句柄。"""

    def __init__(self) -> None:
        super().__init__()
        self.directories: dict[Path, int] = {}


def _fail(code: str = "git_material_worker_invalid") -> NoReturn:
    raise GitMaterialInputError(code)


def _remaining(expiry: int) -> float:
    value = (expiry - time.monotonic_ns()) / 1_000_000_000
    if value <= 0:
        _fail("git_material_worker_timeout")
    return value


def _revision(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_file(descriptor: int, limit: int, expiry: int, *, anonymous: bool = False) -> bytes:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != (0 if anonymous else 1)
        or before.st_size > limit
    ):
        _fail()
    result = bytearray()
    while len(result) <= limit:
        _remaining(expiry)
        chunk = os.read(descriptor, min(65536, limit + 1 - len(result)))
        if not chunk:
            break
        result.extend(chunk)
    after = os.fstat(descriptor)
    if len(result) > limit or len(result) != after.st_size or _revision(before) != _revision(after):
        _fail("git_material_binding_changed")
    return bytes(result)


def _posix_chain(path: Path, resources: _Resources) -> int:
    """逐段打开真实目录，保留FD；不声称阻止任意同UID外部改名。"""
    if path != path.resolve(strict=True):
        _fail("git_material_binding_changed")
    current = Path(path.anchor)
    parent: int | None = None
    for part in (path.anchor, *path.parts[1:]):
        if part != path.anchor:
            current /= part
        descriptor = resources.directories.get(current)
        if descriptor is None:
            descriptor = os.open(part, _READ | os.O_DIRECTORY | _NOFOLLOW, dir_fd=parent)
            resources.callback(os.close, descriptor)
            resources.directories[current] = descriptor
        opened, named = os.fstat(descriptor), os.lstat(current)
        if (opened.st_dev, opened.st_ino, opened.st_mode) != (
            named.st_dev,
            named.st_ino,
            named.st_mode,
        ):
            _fail("git_material_binding_changed")
        parent = descriptor
    assert descriptor is not None
    return descriptor


def _directory(
    path: Path, resources: _Resources, windows: _Windows | None, *, private: bool = False
) -> None:
    if windows is None:
        descriptor = _posix_chain(path, resources)
        if private:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                _fail("git_material_private_invalid")
    else:
        windows.chain(path, resources, resources.directories, private=private)


def _descriptor(
    path: Path, resources: ExitStack, windows: _Windows | None, *, private: bool = False
) -> int:
    if windows is not None:
        return windows.descriptor(path, resources, private=private)
    before = os.lstat(path)
    descriptor = os.open(path, _READ | _NOFOLLOW | _NONBLOCK)
    resources.callback(os.close, descriptor)
    opened = os.fstat(descriptor)
    if (
        _revision(before) != _revision(opened)
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
    ):
        _fail("git_material_binding_changed")
    if private and (opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600):
        _fail("git_material_private_invalid")
    return descriptor


def _control_paths(
    repo: Path, common: Path, resources: _Resources, windows: _Windows | None, expiry: int
) -> tuple[Path, ...]:
    def pointer(path: Path) -> str:
        _directory(path.parent, resources, windows)
        descriptor = _descriptor(path, resources, windows)
        body = _read_file(descriptor, MAX_CONTROL_FILE_BYTES, expiry)
        if b"\0" in body or b"\n" in body.rstrip(b"\r\n") or b"\r" in body.rstrip(b"\r\n"):
            _fail("git_material_namespace_invalid")
        return body.decode("utf-8").strip()

    paths = [common / "config"]
    if os.path.lexists(common / "config.worktree"):
        paths.append(common / "config.worktree")
    gitfile = repo / ".git"
    if stat.S_ISDIR(os.lstat(gitfile).st_mode):
        if gitfile != common:
            _fail("git_material_namespace_invalid")
    else:
        paths.append(gitfile)
        body = pointer(gitfile)
        if not body.startswith("gitdir: "):
            _fail("git_material_namespace_invalid")
        admin = Path(body[8:])
        if not admin.is_absolute():
            admin = repo / admin
        unresolved = admin
        admin = admin.resolve(strict=True)
        if os.path.normpath(str(unresolved)) != str(admin) or admin.parent != common / "worktrees":
            _fail("git_material_namespace_invalid")
        paths.extend((admin / "commondir", admin / "gitdir"))
        common_pointer = admin / pointer(admin / "commondir")
        if (
            os.path.normpath(str(common_pointer)) != str(common)
            or common_pointer.resolve(strict=True) != common
        ):
            _fail("git_material_namespace_invalid")
        backlink = Path(pointer(admin / "gitdir"))
        if (
            os.path.normpath(str(backlink)) != str(gitfile)
            or backlink.resolve(strict=True) != gitfile
        ):
            _fail("git_material_namespace_invalid")
    if os.path.lexists(common / "commondir"):
        _fail("git_material_namespace_invalid")
    return tuple(sorted(paths))


def capture_control_files(repo_path: str, common_path: str) -> tuple[GitControlFileBinding, ...]:
    """宿主只读取得精确控制文件集合；不执行Git或写业务状态。"""
    expiry = time.monotonic_ns() + 30_000_000_000
    windows = _Windows() if os.name == "nt" else None
    result = []
    with _Resources() as resources:
        _directory(Path(repo_path), resources, windows)
        _directory(Path(common_path), resources, windows)
        for path in _control_paths(Path(repo_path), Path(common_path), resources, windows, expiry):
            _directory(path.parent, resources, windows)
            descriptor = _descriptor(path, resources, windows)
            body = _read_file(descriptor, MAX_CONTROL_FILE_BYTES, expiry)
            result.append(
                GitControlFileBinding(
                    str(path),
                    path_identity(str(path), directory=False),
                    len(body),
                    hashlib.sha256(body).hexdigest(),
                )
            )
    return tuple(result)


def _config_value(value: str) -> str:
    """按 Git 单行值语法解释引号、转义和引号外注释，不支持续行。"""
    result: list[str] = []
    quoted, escaped, trim_length = False, False, 0
    escapes = {"t": "\t", "b": "\b", "n": "\n", "\\": "\\", '"': '"'}
    for character in value:
        if escaped:
            if character not in escapes:
                _fail("git_material_configuration_invalid")
            result.append(escapes[character])
            escaped = False
            continue
        if character in " \t\r\v\f" and not quoted:
            if not trim_length:
                trim_length = len(result)
            if result:
                result.append(character)
            continue
        if not quoted and character in "#;":
            break
        trim_length = 0
        if character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        else:
            result.append(character)
    if quoted or escaped:
        _fail("git_material_configuration_invalid")
    if trim_length:
        del result[trim_length:]
    return "".join(result)


def _config_section(text: str) -> str:
    """拒绝原有外部配置来源；带子节的属性不能冒充顶层 extensions。"""
    match = re.fullmatch(r'\[([A-Za-z0-9.-]+)(\s+"[^"\r\n]*")?\]\s*(?:[#;].*)?', text)
    if match is None:
        _fail("git_material_configuration_invalid")
    section = match[1].lower()
    if section in {"include", "includeif", "filter"} or section.startswith(
        ("include.", "includeif.", "filter.")
    ):
        _fail("git_material_configuration_invalid")
    return section if match[2] is None else section + "."


def _configuration(body: bytes, object_format: str, *, require_format: bool = True) -> None:
    """仅解释对象格式所需的安全配置子集；外部include/过滤器/兼容格式拒绝。"""
    if b"\0" in body or b"\\\n" in body or b"\\\r\n" in body:
        _fail("git_material_configuration_invalid")
    section, formats = "", []
    for line in body.decode("utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith(("#", ";")):
            continue
        if text.startswith("["):
            section = _config_section(text)
        elif section == "extensions":
            key, separator, value = text.partition("=")
            key = key.strip().lower()
            if key == "compatobjectformat":
                _fail("git_material_configuration_invalid")
            if key == "objectformat":
                if not separator:
                    _fail("git_material_configuration_invalid")
                formats.append(_config_value(value))
    if not require_format and not formats:
        return
    actual = formats[0] if len(formats) == 1 else "sha1" if not formats else ""
    if actual != object_format:
        _fail("git_material_configuration_invalid")


def _namespace(request: GitMaterialInput, resources: _Resources, windows: _Windows | None) -> None:
    expiry = request.expiry_monotonic_ns
    for path, identity in (
        (request.repo_path, request.repo_identity),
        (request.common_path, request.common_identity),
        (request.objects_path, request.objects_identity),
    ):
        _remaining(expiry)
        _directory(Path(path), resources, windows)
        if path_identity(path, directory=True) != identity:
            _fail("git_material_binding_changed")
    paths = _control_paths(
        Path(request.repo_path), Path(request.common_path), resources, windows, expiry
    )
    if tuple(str(path) for path in paths) != tuple(item.path for item in request.control_files):
        _fail("git_material_namespace_invalid")
    for item in request.control_files:
        _directory(Path(item.path).parent, resources, windows)
        descriptor = _descriptor(Path(item.path), resources, windows)
        body = _read_file(descriptor, MAX_CONTROL_FILE_BYTES, expiry)
        if (
            len(body) != item.size
            or hashlib.sha256(body).hexdigest() != item.sha256
            or path_identity(item.path, directory=False) != item.identity
        ):
            _fail("git_material_binding_changed")
        if Path(item.path).name in {"config", "config.worktree"}:
            _configuration(
                body, request.object_format, require_format=Path(item.path).name == "config"
            )
    objects = Path(request.objects_path)
    for forbidden in (objects / "info/alternates", objects / "info/http-alternates"):
        if os.path.lexists(forbidden):
            _fail("git_material_namespace_invalid")
    _object_entries(objects, expiry, resources, windows)


def _object_entries(
    objects: Path, expiry: int, resources: _Resources, windows: _Windows | None
) -> None:
    """持有已有 fanout/info/pack 目录并拒绝文件别名，读取错误不能静默跳过。"""
    # 逐段观察已有fanout/info/pack；不更改对象库，也不承诺同UID对手的OS封印。
    count = 0

    def walk_error(error: OSError) -> None:
        _fail("git_material_namespace_invalid")

    for current, directories, files in os.walk(objects, followlinks=False, onerror=walk_error):
        _remaining(expiry)
        relative = Path(current).relative_to(objects)
        for name in directories:
            count += 1
            if count > 100000:
                _fail("git_material_namespace_limit")
            if (
                relative == Path(".")
                and name not in {"info", "pack"}
                and re.fullmatch(r"[0-9a-f]{2}", name) is None
            ):
                _fail("git_material_namespace_invalid")
            _directory(Path(current) / name, resources, windows)
        for name in files:
            count += 1
            if count > 100000:
                _fail("git_material_namespace_limit")
            entry = Path(current) / name
            if relative == Path(".") and name in {"info", "pack"}:
                _fail("git_material_namespace_invalid")
            info = os.lstat(entry)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                _fail("git_material_namespace_invalid")
            if windows is not None:
                windows.open(entry, resources, directory=False)


def _snapshot(
    request: GitMaterialInput, resources: _Resources, windows: _Windows | None
) -> BinaryIO:
    """正文写入前建立崩溃清理生命周期，写满/fsync/关闭写端后复读 RO regular。"""
    root, expiry = Path(request.stage_root), request.expiry_monotonic_ns
    _directory(root, resources, windows, private=True)
    if path_identity(str(root), directory=True) != request.stage_root_identity:
        _fail("git_material_binding_changed")
    source = _descriptor(Path(request.body_path), resources, windows, private=True)
    body = _read_file(source, MAX_MATERIAL_BYTES, expiry)
    oid = hashlib.new(
        request.object_format, f"{request.object_type} {len(body)}\0".encode("ascii") + body
    ).hexdigest()
    if (
        len(body) != request.body_bytes
        or hashlib.sha256(body).hexdigest() != request.body_sha256
        or oid != request.expected_oid
    ):
        _fail("git_material_body_changed")
    path = root / f"snapshot-{request.nonce}-{secrets.token_hex(16)}.bin"
    with ExitStack() as writers:
        if windows is None:
            write_fd, descriptor = _posix_snapshot(path, writers, resources)
        else:
            write_fd = windows.snapshot_writer(path, writers)
        _write_snapshot(write_fd, body, expiry)
        if windows is not None:
            descriptor = windows.snapshot_reader(write_fd, resources)
        # writers 结算关闭唯一写端；RO 端才允许进入复读及 Git 启动阶段。
    os.lseek(descriptor, 0, os.SEEK_SET)
    observed = _read_file(descriptor, MAX_MATERIAL_BYTES, expiry, anonymous=windows is None)
    if observed != body:
        _fail("git_material_body_changed")
    os.lseek(descriptor, 0, os.SEEK_SET)
    # 只复制已降为 RO 的 FD；Windows 同一 FILE_OBJECT 保留 DELETE_ON_CLOSE。
    stream = os.fdopen(os.dup(descriptor), "rb", buffering=0)
    resources.callback(stream.close)
    return stream


def _posix_snapshot(path: Path, writers: ExitStack, resources: ExitStack) -> tuple[int, int]:
    """排他建空文件、开 RO、立即去名；任何正文写入都发生在 unlink 之后。"""
    writer = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    writers.callback(os.close, writer)
    owned = os.fstat(writer)
    try:
        reader = _descriptor(path, resources, None, private=True)
        opened = os.fstat(reader)
        if (opened.st_dev, opened.st_ino) != (owned.st_dev, owned.st_ino):
            _fail("git_material_binding_changed")
    finally:
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
            _fail("git_material_binding_changed")
        path.unlink()
    return writer, reader


def _write_snapshot(descriptor: int, body: bytes, expiry: int) -> None:
    """处理部分写入并完整 fsync；失败不向 Git 发布半成品读端。"""
    offset = 0
    while offset < len(body):
        _remaining(expiry)
        written = os.write(descriptor, body[offset : offset + 65536])
        if written <= 0:
            _fail()
        offset += written
    os.fsync(descriptor)
