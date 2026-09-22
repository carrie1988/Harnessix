"""以最后提交标记发布Soak Run，并从磁盘独立校验证据。"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel
from scripts.soak_manifest import SoakManifest, verify_manifest_samples
from scripts.soak_sample_file import SAMPLE_FILENAME, write_sample_file
from scripts.soak_samples import SoakSample

MANIFEST_FILENAME = "manifest.json"
COMMIT_FILENAME = "COMMITTED.json"
MAX_MANIFEST_BYTES = 128 * 1024
MAX_COMMIT_BYTES = 1024
_RUN_FILES = frozenset({SAMPLE_FILENAME, MANIFEST_FILENAME, COMMIT_FILENAME})


class SoakCommit(ContractModel):
    """只绑定Manifest原始字节摘要，避免自引用。"""

    spec_version: Literal["harnessix.soak-commit/v1"]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _ensure_private_root(path: Path) -> None:
    """在POSIX检查0700所有权，Windows拒绝重解析目录。"""

    try:
        if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
            raise OSError
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError
        if os.name == "posix" and (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise OSError
    except OSError:
        raise KernelError("soak_evidence_root_invalid", "Soak证据根目录不安全") from None


def _sync_directory(path: Path) -> None:
    """POSIX同步目录元数据；Windows靠重启后重新校验提交标记。"""

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_file(directory: Path, name: str, body: bytes, max_bytes: int) -> None:
    if not 0 < len(body) <= max_bytes:
        raise KernelError("soak_evidence_invalid", "Soak证据文件大小无效")
    target = directory / name
    temporary = directory / f".{name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        if target.exists() or target.is_symlink():
            raise OSError
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if target.exists() or target.is_symlink():
            raise OSError
        os.replace(temporary, target)
        _sync_directory(directory)
    except OSError:
        raise KernelError("soak_evidence_write_failed", "Soak证据写入失败") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _read_file(directory: Path, name: str, max_bytes: int) -> bytes:
    target = directory / name
    try:
        info = target.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= max_bytes:
            raise OSError
        body = target.read_bytes()
        if len(body) != info.st_size or not body.endswith(b"\n"):
            raise OSError
    except OSError:
        raise KernelError("soak_evidence_invalid", "Soak证据文件无效") from None
    return body


def publish_run(
    evidence_root: Path,
    manifest: SoakManifest,
    samples: tuple[SoakSample, ...],
    *,
    fault: Callable[[str], None] | None = None,
) -> tuple[Path, str]:
    """排他创建Run目录；样本、Manifest校验后最后写入提交标记。"""

    _ensure_private_root(evidence_root)
    run_directory = evidence_root / manifest.run_id
    try:
        run_directory.mkdir(mode=0o700, exist_ok=False)
        _sync_directory(evidence_root)
    except OSError:
        raise KernelError("soak_run_exists", "Soak Run目录已存在或不可创建") from None
    digest, statistics = write_sample_file(
        run_directory,
        samples,
        run_id=manifest.run_id,
        scenario_id=manifest.scenario_id,
        expected_measured=manifest.sample_counts,
    )
    if digest != manifest.evidence_sha256[SAMPLE_FILENAME] or statistics != manifest.statistics:
        raise KernelError("soak_manifest_mismatch", "Soak Manifest与样本不一致")
    verify_manifest_samples(manifest, run_directory)
    if fault is not None:
        fault("after_samples")
    body = (manifest.model_dump_json() + "\n").encode("utf-8")
    _write_file(run_directory, MANIFEST_FILENAME, body, MAX_MANIFEST_BYTES)
    manifest_digest = sha256(body).hexdigest()
    if fault is not None:
        fault("after_manifest")
    commit = SoakCommit(
        spec_version="harnessix.soak-commit/v1",
        manifest_sha256=manifest_digest,
    )
    marker = (commit.model_dump_json() + "\n").encode("utf-8")
    if fault is not None:
        fault("before_commit")
    _write_file(run_directory, COMMIT_FILENAME, marker, MAX_COMMIT_BYTES)
    return run_directory, manifest_digest


def read_published_run(run_directory: Path) -> tuple[SoakManifest, str]:
    """提交标记、Manifest和样本全部可重算时才接受Run。"""

    try:
        if not stat.S_ISDIR(run_directory.stat(follow_symlinks=False).st_mode):
            raise OSError
        if {path.name for path in run_directory.iterdir()} != _RUN_FILES:
            raise OSError
        marker_body = _read_file(run_directory, COMMIT_FILENAME, MAX_COMMIT_BYTES)
        marker = SoakCommit.model_validate_json(marker_body)
        if marker_body != (marker.model_dump_json() + "\n").encode("utf-8"):
            raise ValueError
        manifest_body = _read_file(run_directory, MANIFEST_FILENAME, MAX_MANIFEST_BYTES)
        if sha256(manifest_body).hexdigest() != marker.manifest_sha256:
            raise ValueError
        manifest = SoakManifest.model_validate_json(manifest_body)
        if manifest_body != (manifest.model_dump_json() + "\n").encode("utf-8"):
            raise ValueError
        if run_directory.name != manifest.run_id:
            raise ValueError
        verify_manifest_samples(manifest, run_directory)
    except (OSError, ValueError, ValidationError, KernelError):
        raise KernelError("soak_run_invalid", "Soak Run提交证据无效") from None
    return manifest, marker.manifest_sha256
