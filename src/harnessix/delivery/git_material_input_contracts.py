"""Git 对象私有材料握手：仅使用标准库的固定合同和规范字节编码。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal, cast

from harnessix.delivery.git_material_trace2_profile import (
    GitMaterialTrace2Mode,
    validate_material_trace2,
    validate_material_trace2_environment,
)

MAX_MATERIAL_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CONTROL_FILE_BYTES = 1024 * 1024
MAX_PROOF_BYTES = 4096
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class GitMaterialInputError(ValueError):
    """固定低敏错误；调用方自行映射到原产品错误，不保留第三方异常。"""

    def __init__(self, code: str = "git_material_input_invalid") -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str = "git_material_input_invalid") -> None:
    if not condition:
        raise GitMaterialInputError(code)


def _digest(value: object) -> None:
    _require(type(value) is str and _DIGEST.fullmatch(value) is not None)


def _path(value: object) -> None:
    _require(
        type(value) is str
        and 1 <= len(value) <= 4096
        and os.path.isabs(value)
        and os.path.normpath(value) == value
        and not any(ord(item) < 32 or 0xD800 <= ord(item) <= 0xDFFF for item in value)
    )


def _integer(value: object, low: int, high: int) -> None:
    _require(type(value) is int and low <= value <= high)


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        + "\n"
    ).encode("ascii")


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        _require(key not in result)
        result[key] = value
    return result


def _decode(body: bytes, limit: int) -> dict[str, Any]:
    _require(type(body) is bytes and 0 < len(body) <= limit)
    try:
        result = json.loads(body.decode("ascii"), object_pairs_hook=_pairs)
        _require(type(result) is dict and _json_bytes(result) == body)
        # JSON 边界保留动态类型；下游逐字段严格验证后才生成冻结合同。
        return cast(dict[str, Any], result)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise GitMaterialInputError() from None


def _keys(value: dict[str, object], model: type) -> None:
    _require(set(value) == {item.name for item in fields(model)})


@dataclass(frozen=True, slots=True)
class GitControlFileBinding:
    """Git 定位/配置控制文件的完整版本，正文不进入握手。"""

    path: str = field(repr=False)
    identity: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _path(self.path)
        _digest(self.identity)
        _integer(self.size, 0, MAX_CONTROL_FILE_BYTES)
        _digest(self.sha256)


@dataclass(frozen=True, slots=True)
class GitMaterialInput:
    """正式批准绑定的单次对象写用途；不授权引用、提交或推送操作。"""

    nonce: str
    source_digest: str
    stage_root: str = field(repr=False)
    stage_root_identity: str
    body_ref: str = field(repr=False)
    body_path: str = field(repr=False)
    body_sha256: str
    body_bytes: int
    expected_oid: str
    object_format: Literal["sha1", "sha256"]
    repo_path: str = field(repr=False)
    repo_identity: str
    common_path: str = field(repr=False)
    common_identity: str
    objects_path: str = field(repr=False)
    objects_identity: str
    control_files: tuple[GitControlFileBinding, ...] = field(repr=False)
    git_argv: tuple[str, ...] = field(repr=False)
    git_environment: tuple[tuple[str, str], ...] = field(repr=False)
    git_executable_identity: str
    expiry_monotonic_ns: int
    implementation_digest: str
    purpose_digest: str
    object_type: Literal["blob", "tree", "commit"] = "blob"
    version: Literal["harnessix.git-material-input/v1", "harnessix.git-material-input/v2"] = (
        "harnessix.git-material-input/v1"
    )
    purpose: Literal["git-object-write"] = "git-object-write"
    trace2_mode: GitMaterialTrace2Mode = "off"
    trace2_profile_sha256: str = ""

    @classmethod
    def create(cls, **values: Any) -> GitMaterialInput:
        """先计算排除自身的摘要，再经正常构造器完整验真，不伪造冻结实例。"""
        try:
            value = dict(values)
            _require("purpose_digest" not in value)
            for name, default in (
                ("object_type", "blob"),
                ("version", "harnessix.git-material-input/v1"),
                ("purpose", "git-object-write"),
                ("trace2_mode", "off"),
                ("trace2_profile_sha256", ""),
            ):
                value.setdefault(name, default)
            controls = value["control_files"]
            _require(type(controls) is tuple)
            encoded = dict(value)
            if value["version"] == "harnessix.git-material-input/v1":
                del encoded["trace2_mode"], encoded["trace2_profile_sha256"]
            encoded["control_files"] = [
                {item.name: getattr(control, item.name) for item in fields(control)}
                for control in controls
            ]
            value["purpose_digest"] = hashlib.sha256(_json_bytes(encoded)).hexdigest()
            return cls(**value)
        except (ValueError, TypeError, AttributeError, KeyError, UnicodeError, RecursionError):
            raise GitMaterialInputError() from None

    def __post_init__(self) -> None:
        _validate_input(self)

    def binding(self) -> dict[str, object]:
        """完整用途字段；产品调用方必须把该字段集纳入原正式 Plan。"""
        result = {item.name: getattr(self, item.name) for item in fields(self)}
        if self.version == "harnessix.git-material-input/v1":
            del result["trace2_mode"], result["trace2_profile_sha256"]
        result["control_files"] = [
            {item.name: getattr(control, item.name) for item in fields(control)}
            for control in self.control_files
        ]
        return result


def _validate_input(request: GitMaterialInput) -> None:
    for name in (
        "nonce",
        "source_digest",
        "stage_root_identity",
        "body_sha256",
        "repo_identity",
        "common_identity",
        "objects_identity",
        "git_executable_identity",
        "implementation_digest",
        "purpose_digest",
    ):
        _digest(getattr(request, name))
    for name in ("stage_root", "body_path", "repo_path", "common_path", "objects_path"):
        _path(getattr(request, name))
    _integer(request.body_bytes, 0, MAX_MATERIAL_BYTES)
    _integer(request.expiry_monotonic_ns, 1, 2**63 - 1)
    _require(type(request.object_format) is str and request.object_format in {"sha1", "sha256"})
    _require(type(request.object_type) is str and request.object_type in {"blob", "tree", "commit"})
    validate_material_trace2(request.trace2_mode, request.trace2_profile_sha256)
    expected_version = (
        "harnessix.git-material-input/v1"
        if request.trace2_mode == "off"
        else "harnessix.git-material-input/v2"
    )
    _require(type(request.version) is str and request.version == expected_version)
    _require(type(request.purpose) is str and request.purpose == "git-object-write")
    length = 40 if request.object_format == "sha1" else 64
    _require(
        type(request.expected_oid) is str
        and re.fullmatch(f"[0-9a-f]{{{length}}}", request.expected_oid) is not None
    )
    _require(type(request.body_ref) is str and request.body_ref == f"body-{request.nonce}.bin")
    _require(request.body_path == os.path.join(request.stage_root, request.body_ref))
    _require(request.objects_path == os.path.join(request.common_path, "objects"))
    _validate_sequences(request)


def _validate_sequences(request: GitMaterialInput) -> None:
    _require(type(request.control_files) is tuple and 1 <= len(request.control_files) <= 8)
    for item in request.control_files:
        _require(type(item) is GitControlFileBinding)
        item.__post_init__()
    _require(
        tuple(item.path for item in request.control_files)
        == tuple(sorted({item.path for item in request.control_files}))
    )
    _require(type(request.git_argv) is tuple and 1 <= len(request.git_argv) <= 128)
    _require(all(type(item) is str and item and "\0" not in item for item in request.git_argv))
    _path(request.git_argv[0])
    _require(sum(len(item.encode("utf-8")) + 1 for item in request.git_argv) <= MAX_MANIFEST_BYTES)
    _require(type(request.git_environment) is tuple and 1 <= len(request.git_environment) <= 32)
    for pair in request.git_environment:
        _require(type(pair) is tuple and len(pair) == 2)
        _require(all(type(item) is str and "\0" not in item for item in pair))
        _require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", pair[0]) is not None)
    _require(request.git_environment == tuple(sorted(dict(request.git_environment).items())))
    validate_material_trace2_environment(
        request.trace2_mode, request.trace2_profile_sha256, dict(request.git_environment)
    )
    _require(purpose_digest(request) == request.purpose_digest)


def purpose_digest(request: GitMaterialInput) -> str:
    """用途摘要排除自身，但包含 nonce、绝对期限及完整路径/程序绑定。"""
    value = request.binding()
    del value["purpose_digest"]
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def encode_manifest(request: GitMaterialInput) -> bytes:
    _require(type(request) is GitMaterialInput)
    request.__post_init__()
    body = _json_bytes(request.binding())
    _require(len(body) <= MAX_MANIFEST_BYTES)
    return body


def _input_wire_keys(value: dict[str, object]) -> None:
    """两份完整精确字段集；v1不能借新增内存默认字段接受wire扩展。"""
    keys = {item.name for item in fields(GitMaterialInput)}
    version = value.get("version")
    if type(version) is str and version == "harnessix.git-material-input/v1":
        keys -= {"trace2_mode", "trace2_profile_sha256"}
    else:
        _require(type(version) is str and version == "harnessix.git-material-input/v2")
    _require(set(value) == keys)


def decode_manifest(payload: bytes) -> GitMaterialInput:
    value = _decode(payload, MAX_MANIFEST_BYTES)
    _input_wire_keys(value)
    try:
        controls = value["control_files"]
        _require(type(controls) is list)
        for item in controls:
            _require(type(item) is dict)
            _keys(item, GitControlFileBinding)
        value["control_files"] = tuple(GitControlFileBinding(**item) for item in controls)
        _require(type(value["git_argv"]) is list and type(value["git_environment"]) is list)
        _require(all(type(pair) is list for pair in value["git_environment"]))
        value["git_argv"] = tuple(value["git_argv"])
        value["git_environment"] = tuple(tuple(pair) for pair in value["git_environment"])
        return GitMaterialInput(**value)
    except (ValueError, TypeError, AttributeError, KeyError, UnicodeError):
        raise GitMaterialInputError() from None


def manifest_sha256(request: GitMaterialInput) -> str:
    return hashlib.sha256(encode_manifest(request)).hexdigest()


@dataclass(frozen=True, slots=True)
class GitMaterialProof:
    """模块的生产者证明；原 Owner 认证这些字节，不直接观察 Git 输入。"""

    nonce: str
    source_digest: str
    manifest_sha256: str
    purpose_digest: str
    implementation_digest: str
    body_sha256: str
    body_bytes: int
    source_eof: bool
    snapshot_sha256: str
    snapshot_bytes: int
    object_id: str
    object_format: Literal["sha1", "sha256"]
    git_stdout_eof: bool
    git_returncode: int
    producer_pid: int
    object_type: Literal["blob", "tree", "commit"] = "blob"
    version: Literal["harnessix.git-material-proof/v1"] = "harnessix.git-material-proof/v1"

    def __post_init__(self) -> None:
        for name in (
            "nonce",
            "source_digest",
            "manifest_sha256",
            "purpose_digest",
            "implementation_digest",
            "body_sha256",
            "snapshot_sha256",
        ):
            _digest(getattr(self, name))
        _integer(self.body_bytes, 0, MAX_MATERIAL_BYTES)
        _integer(self.snapshot_bytes, 0, MAX_MATERIAL_BYTES)
        _integer(self.producer_pid, 1, 2**32 - 1)
        _require(type(self.source_eof) is bool and self.source_eof)
        _require(type(self.git_stdout_eof) is bool and self.git_stdout_eof)
        _require(type(self.git_returncode) is int and self.git_returncode == 0)
        _require(type(self.object_format) is str and self.object_format in {"sha1", "sha256"})
        _require(type(self.object_type) is str and self.object_type in {"blob", "tree", "commit"})
        _require(type(self.version) is str and self.version == "harnessix.git-material-proof/v1")
        length = 40 if self.object_format == "sha1" else 64
        _require(
            type(self.object_id) is str
            and re.fullmatch(f"[0-9a-f]{{{length}}}", self.object_id) is not None
        )
        _require(
            self.snapshot_sha256 == self.body_sha256 and self.snapshot_bytes == self.body_bytes
        )


def encode_proof(proof: GitMaterialProof) -> bytes:
    _require(type(proof) is GitMaterialProof)
    proof.__post_init__()
    body = _json_bytes({item.name: getattr(proof, item.name) for item in fields(proof)})
    _require(len(body) <= MAX_PROOF_BYTES)
    return body


def decode_proof(payload: bytes, request: GitMaterialInput) -> GitMaterialProof:
    _require(type(request) is GitMaterialInput)
    request.__post_init__()
    value = _decode(payload, MAX_PROOF_BYTES)
    _keys(value, GitMaterialProof)
    try:
        proof = GitMaterialProof(**value)
        for name in (
            "nonce",
            "source_digest",
            "purpose_digest",
            "implementation_digest",
            "body_sha256",
            "body_bytes",
            "object_format",
            "object_type",
        ):
            _require(getattr(proof, name) == getattr(request, name))
        _require(
            proof.object_id == request.expected_oid
            and proof.manifest_sha256 == manifest_sha256(request)
        )
        return proof
    except (ValueError, TypeError, AttributeError):
        raise GitMaterialInputError("git_material_proof_invalid") from None


def implementation_digest() -> str:
    """绑定全部六个模块及两个包入口；宿主另绑定 runtime/import root，不伪造Owner能力。"""
    try:
        directory = Path(__file__).parent
        package = directory.parent
        value = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in (
                ("harnessix/__init__.py", package / "__init__.py"),
                ("harnessix/delivery/__init__.py", directory / "__init__.py"),
                (
                    "harnessix/delivery/git_material_input_contracts.py",
                    directory / "git_material_input_contracts.py",
                ),
                ("harnessix/delivery/git_material_worker.py", directory / "git_material_worker.py"),
                (
                    "harnessix/delivery/git_material_trace2_profile.py",
                    directory / "git_material_trace2_profile.py",
                ),
                (
                    "harnessix/delivery/git_material_failure.py",
                    directory / "git_material_failure.py",
                ),
                ("harnessix/delivery/git_material_native.py", directory / "git_material_native.py"),
                (
                    "harnessix/delivery/git_material_native_windows.py",
                    directory / "git_material_native_windows.py",
                ),
            )
        }
        return hashlib.sha256(_json_bytes(value)).hexdigest()
    except OSError:
        raise GitMaterialInputError("git_material_implementation_unavailable") from None


def path_identity(path: str, *, directory: bool) -> str:
    """与原领域路径身份算法一致；观察不是任意同 UID 破坏的 OS 封印。"""
    try:
        _path(path)
        info = os.lstat(path)
        _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        value = {
            "path": os.path.normcase(str(Path(path).resolve(strict=True))),
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode_type": stat.S_IFMT(info.st_mode),
        }
        return hashlib.sha256(
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    except OSError:
        raise GitMaterialInputError("git_material_binding_changed") from None


def executable_identity(path: str) -> str:
    try:
        _path(path)
        info = os.stat(path)
        _require(stat.S_ISREG(info.st_mode) and os.access(path, os.X_OK))
        value = {
            "path": os.path.normcase(str(Path(path).resolve(strict=True))),
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
            "mode": info.st_mode,
        }
        return hashlib.sha256(
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    except OSError:
        raise GitMaterialInputError("git_material_binding_changed") from None
