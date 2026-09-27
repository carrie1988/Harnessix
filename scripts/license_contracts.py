"""锁定发行物与原字节证据合同；不读取已安装发行版元数据。"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.secret_scan import _read_file
from scripts.secret_scan_contracts import ScanBudget, ScanIncompleteError, ScanLimits


class LicenseEvidenceError(ValueError):
    """只能公开固定错误码；不得拼入归档正文、网络异常或本地路径。"""


@dataclass(frozen=True)
class LockedArchive:
    """一个具体发行文件的不可替换身份；平台变体不能共用未经验证的结论。"""

    name: str
    version: str
    registry: str
    kind: str
    url: str
    sha256: str
    size_bytes: int

    def identity(self) -> dict[str, object]:
        return self.__dict__.copy()


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical_bytes(document: object) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def read_input(path: Path, *, cap: int = 16 * 1024 * 1024) -> bytes:
    """复用既有普通文件身份/链接/变更防护，不把读取失败变成空证据。"""

    try:
        return _read_file(path, ScanBudget(ScanLimits(file_bytes=cap), time.monotonic()))
    except ScanIncompleteError:
        raise LicenseEvidenceError("license_input_unreadable") from None


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise LicenseEvidenceError("license_json_duplicate_key")
        document[key] = value
    return document


def read_json(path: Path) -> dict:
    try:
        document = json.loads(read_input(path), object_pairs_hook=_unique_keys)
    except (UnicodeError, json.JSONDecodeError):
        raise LicenseEvidenceError("license_json_invalid") from None
    if not isinstance(document, dict):
        raise LicenseEvidenceError("license_json_invalid")
    return document


def locked_inventory(lock_path: Path, project_path: Path) -> tuple[dict, list[LockedArchive]]:
    """固定当前PyPI锁合同；拒绝自有包冒名、重复身份、未知来源或没有Archive。"""

    try:
        lock = tomllib.loads(read_input(lock_path).decode("utf-8"))
        project = tomllib.loads(read_input(project_path).decode("utf-8"))["project"]
        packages = lock["package"]
        if not isinstance(packages, list) or not 1 <= len(packages) <= 512:
            raise LicenseEvidenceError("license_lock_invalid")
        root_name, root_version = project["name"], project["version"]
        Version(root_version)
        archives, seen, hashes, roots = [], set(), set(), 0
        for package in packages:
            name, version, source = package["name"], package["version"], package["source"]
            if not isinstance(name, str) or canonicalize_name(name, validate=True) != name:
                raise LicenseEvidenceError("license_lock_invalid")
            Version(version)
            if name in seen:
                raise LicenseEvidenceError("license_lock_duplicate")
            seen.add(name)
            if name == root_name:
                if version != root_version or source != {"editable": "."}:
                    raise LicenseEvidenceError("license_root_identity_mismatch")
                if package.get("wheels") or package.get("sdist"):
                    raise LicenseEvidenceError("license_root_identity_mismatch")
                roots += 1
                continue
            if source != {"registry": "https://pypi.org/simple"}:
                raise LicenseEvidenceError("license_source_unsupported")
            items = [("wheel", item) for item in package.get("wheels", [])]
            if "sdist" in package:
                items.append(("sdist", package["sdist"]))
            if not items:
                raise LicenseEvidenceError("license_archive_missing")
            for kind, item in items:
                url, sha, size = item["url"], item["hash"], item["size"]
                parsed = urlsplit(url)
                if (
                    parsed.scheme != "https"
                    or parsed.netloc != "files.pythonhosted.org"
                    or not parsed.path.startswith("/packages/")
                    or parsed.query
                    or parsed.fragment
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", sha)
                    or type(size) is not int
                    or not 0 < size <= 32 * 1024 * 1024
                ):
                    raise LicenseEvidenceError("license_archive_invalid")
                if sha in hashes:
                    raise LicenseEvidenceError("license_archive_duplicate")
                hashes.add(sha)
                archives.append(
                    LockedArchive(name, version, source["registry"], kind, url, sha[7:], size)
                )
        if roots != 1 or len(archives) > 4096 or sum(a.size_bytes for a in archives) > 2 * 1024**3:
            raise LicenseEvidenceError("license_lock_invalid")
    except LicenseEvidenceError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, InvalidVersion):
        raise LicenseEvidenceError("license_lock_invalid") from None
    return project, sorted(archives, key=lambda a: (a.name, a.version, a.url))


def blob_record(body: bytes, member: str) -> dict:
    return {"member": member, "sha256": digest(body), "size_bytes": len(body)}


def read_blob(directory: Path, record: dict) -> bytes:
    """证据文件名只由摘要生成；成员名称从不解释为宿主路径。"""

    sha, size = record.get("sha256"), record.get("size_bytes")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise LicenseEvidenceError("license_blob_invalid")
    if type(size) is not int or not 0 < size <= 1024 * 1024:
        raise LicenseEvidenceError("license_blob_invalid")
    body = read_input(directory / "blobs" / sha, cap=1024 * 1024)
    if digest(body) != sha or len(body) != size:
        raise LicenseEvidenceError("license_blob_mismatch")
    return body


class EvidenceBlobs:
    """一次离线报告共享去重缓存与64MiB/4096项/60秒预算，避免重复读取放大。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.started = time.monotonic()
        self.bodies: dict[str, bytes] = {}
        self.bytes_read = 0

    def read(self, record: dict) -> bytes:
        if time.monotonic() - self.started > 60:
            raise LicenseEvidenceError("license_verification_timeout")
        sha = record.get("sha256")
        if not isinstance(sha, str):
            raise LicenseEvidenceError("license_blob_invalid")
        if sha not in self.bodies:
            if len(self.bodies) >= 4096:
                raise LicenseEvidenceError("license_evidence_limit")
            body = read_blob(self.directory, record)
            self.bytes_read += len(body)
            if self.bytes_read > 64 * 1024**2:
                raise LicenseEvidenceError("license_evidence_limit")
            self.bodies[sha] = body
        body = self.bodies[sha]
        if type(record.get("size_bytes")) is not int or len(body) != record["size_bytes"]:
            raise LicenseEvidenceError("license_blob_mismatch")
        return body
