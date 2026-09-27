"""显式采集uv.lock全部发行Archive；仅读取字节，绝不安装或执行上游代码。"""

from __future__ import annotations

import argparse
import io
import os
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

if not __package__:
    # 独立脚本也以scripts包导入共享读取器，避免依赖调用方PYTHONPATH。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.cli_console import configure_utf8_console
from scripts.license_contracts import (
    LicenseEvidenceError,
    LockedArchive,
    blob_record,
    canonical_bytes,
    digest,
    locked_inventory,
    read_input,
)
from scripts.license_decisions import declared_notice_members
from scripts.secret_scan_archives import _tar_members, archive_kind, archive_members
from scripts.secret_scan_contracts import ScanBudget, ScanIncompleteError, ScanLimits

EVIDENCE_DIRECTORY = "governance/license-evidence-v2"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        raise LicenseEvidenceError("license_download_redirect")


def _safe_directory(path: Path) -> None:
    """输出必须处于普通目录，不能借由归档名或已存在链接写出范围。"""

    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (
            candidate.exists() and getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise LicenseEvidenceError("license_output_unsupported")
    path.mkdir(parents=True, exist_ok=True)


def write_atomic(path: Path, body: bytes) -> None:
    """每个证据Blob/索引原子替换；批次未完成时不会发布新的索引。"""

    _safe_directory(path.parent)
    descriptor, name = tempfile.mkstemp(prefix=".license-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def download_archive(archive: LockedArchive) -> bytes:
    """只访问锁定官方URL，无凭据、无环境代理、无重定向、无自动重试。"""

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    started = time.monotonic()
    body = io.BytesIO()
    try:
        with opener.open(archive.url, timeout=20) as response:
            if response.status != 200 or response.geturl() != archive.url:
                raise LicenseEvidenceError("license_download_invalid")
            while chunk := response.read(min(65536, archive.size_bytes + 1 - body.tell())):
                if time.monotonic() - started > 60 or body.tell() + len(chunk) > archive.size_bytes:
                    raise LicenseEvidenceError("license_download_limit")
                body.write(chunk)
    except (OSError, urllib.error.URLError):
        raise LicenseEvidenceError("license_download_failed") from None
    result = body.getvalue()
    if len(result) != archive.size_bytes or digest(result) != archive.sha256:
        raise LicenseEvidenceError("license_archive_mismatch")
    return result


def archive_evidence(body: bytes, archive: LockedArchive) -> tuple[dict, dict[str, bytes]]:
    """每个Archive独立校验，再保留原METADATA/PKG-INFO与候选许可证文本。"""

    if len(body) != archive.size_bytes or digest(body) != archive.sha256:
        raise LicenseEvidenceError("license_archive_mismatch")
    # 第三方源码包与二进制Wheel的预算独立于产品Secret扫描，不改变其默认上限。
    limits = ScanLimits(
        member_bytes=64 * 1024**2,
        expanded_bytes=256 * 1024**2,
        total_bytes=512 * 1024**2,
        archive_entries=20000,
        entries=20002,
        central_directory_bytes=8 * 1024**2,
    )
    budget = ScanBudget(limits, time.monotonic())
    selected: dict[str, bytes] = {}
    metadata: list[str] = []
    names: set[str] = set()
    try:
        kind = archive_kind(body, archive.url)
        if kind not in ("zip", "gz", "tar") or (archive.kind == "wheel" and kind != "zip"):
            raise LicenseEvidenceError("license_archive_unsupported")
        if kind == "gz":
            # 复用流预算和完整结束检查，再进入TAR头预检；不调用文件系统extract。
            expanded = list(archive_members(body, archive.url, kind, budget))
            if len(expanded) != 1:
                raise LicenseEvidenceError("license_archive_invalid")
            body, kind = expanded[0][1], "tar"
        members = (
            _tar_members(body, budget, skip_links=True)
            if kind == "tar"
            else archive_members(body, archive.url, kind, budget)
        )
        for member, content in members:
            path = PurePosixPath(member)
            if member in names:
                raise LicenseEvidenceError("license_archive_duplicate_member")
            names.add(member)
            is_meta = (
                archive.kind == "wheel"
                and len(path.parts) == 2
                and path.parts[0].endswith(".dist-info")
                and path.name == "METADATA"
            ) or (archive.kind == "sdist" and len(path.parts) == 2 and path.name == "PKG-INFO")
            is_notice = (
                path.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))
                or "licenses" in path.parts
            )
            if is_meta or is_notice:
                if (
                    not 0 < len(content) <= 1024**2
                    or len(selected) >= 128
                    or sum(map(len, selected.values())) + len(content) > 8 * 1024**2
                ):
                    raise LicenseEvidenceError("license_evidence_limit")
                selected[member] = content
            if is_meta:
                metadata.append(member)
        if len(metadata) != 1:
            raise LicenseEvidenceError("license_metadata_count")
        required = declared_notice_members(selected[metadata[0]], archive.kind, metadata[0], names)
        missing = set(required) - selected.keys()
        if missing:
            # License-File可声明AUTHORS或任意文件名；不能只按LICENSE后缀猜测范围。
            members = (
                _tar_members(body, budget, skip_links=True)
                if kind == "tar"
                else archive_members(body, archive.url, kind, budget)
            )
            for member, content in members:
                if member in missing:
                    if not 0 < len(content) <= 1024**2:
                        raise LicenseEvidenceError("license_evidence_limit")
                    selected[member] = content
            if missing - selected.keys():
                raise LicenseEvidenceError("license_declared_notice_missing")
    except ScanIncompleteError:
        raise LicenseEvidenceError("license_archive_invalid") from None
    blobs = {digest(content): content for content in selected.values()}
    record = {
        **archive.identity(),
        "metadata": blob_record(selected[metadata[0]], metadata[0]),
        "notices": [
            blob_record(content, member)
            for member, content in sorted(selected.items())
            if member != metadata[0]
        ],
    }
    return record, blobs


def collect_inventory(
    lock: Path, project: Path, directory: Path, cache: Path, *, fetch: bool
) -> dict:
    """四个有界Worker采集全部锁定文件；缺件/失败/取消均保留旧索引并阻断。"""

    snapshot = read_input(lock)
    _, archives = locked_inventory(lock, project)
    _safe_directory(cache)
    _safe_directory(directory / "blobs")
    stopped, guard = threading.Event(), threading.Lock()
    started = time.monotonic()
    seen_blobs: set[str] = set()
    blob_bytes = 0

    def collect(archive: LockedArchive) -> dict:
        nonlocal blob_bytes
        if stopped.is_set() or time.monotonic() - started > 600:
            raise LicenseEvidenceError("license_collection_cancelled")
        cached = cache / archive.sha256
        if cached.exists() or cached.is_symlink():
            body = read_input(cached, cap=32 * 1024**2)
        elif fetch:
            body = download_archive(archive)
            write_atomic(cached, body)
        else:
            raise LicenseEvidenceError("license_cache_missing")
        record, blobs = archive_evidence(body, archive)
        with guard:
            for sha, content in blobs.items():
                if sha in seen_blobs:
                    continue
                blob_bytes += len(content)
                if blob_bytes > 64 * 1024**2 or len(seen_blobs) >= 4096:
                    raise LicenseEvidenceError("license_evidence_limit")
                write_atomic(directory / "blobs" / sha, content)
                seen_blobs.add(sha)
        return record

    pool = ThreadPoolExecutor(max_workers=4)

    def guarded_collect(archive: LockedArchive) -> dict:
        try:
            return collect(archive)
        except BaseException:
            # 仅传播异常与通知停止；不吞异常，不再为后续排队项开始新下载。
            stopped.set()
            raise

    pending = [pool.submit(guarded_collect, archive) for archive in archives]
    try:
        entries = [future.result() for future in pending]
    finally:
        stopped.set()
        pool.shutdown(wait=True, cancel_futures=True)
    if read_input(lock) != snapshot:
        raise LicenseEvidenceError("license_lock_changed")
    index = {
        "spec_version": "harnessix.license-evidence/v2",
        "lock_sha256": digest(snapshot),
        "archive_count": len(entries),
        "entries": entries,
        "collector_version": "harnessix.license-inventory/v2",
    }
    write_atomic(directory / "index.json", canonical_bytes(index))
    return index


def main(argv: Sequence[str] | None = None) -> int:
    configure_utf8_console()
    parser = argparse.ArgumentParser(description="锁定发行物许可证据采集（默认仅使用缓存）")
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--evidence", type=Path, default=Path(EVIDENCE_DIRECTORY))
    parser.add_argument("--cache", type=Path, default=Path("build/license-archives"))
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args(argv)
    try:
        index = collect_inventory(
            args.lock, args.project, args.evidence, args.cache, fetch=args.fetch
        )
    except KeyboardInterrupt:
        print("许可证据采集未完成：license_cancelled", file=sys.stderr)
        return 2
    except LicenseEvidenceError as error:
        print(f"许可证据采集未完成：{error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError):
        print("许可证据采集未完成：license_collection_failed", file=sys.stderr)
        return 2
    print(f"许可证据采集完成：{index['archive_count']}个锁定Archive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
