"""工作区封存的有界合同、规范序列化与域隔离 HMAC；不持久化密钥。"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel
from harnessix.evals.swebench.contracts import SWE_BENCH_CASE_COUNT
from harnessix.evals.swebench.predictions import _MAX_PATCH_BYTES

SEALED_WORKSPACE_FILE = "sealed-workspace.json"
RETIRED_WORKSPACE_MARKER = ".harnessix-retired.json"
_STATE_FILE = "case-state.json"
_SESSION_FILE = "session.sqlite"
_MAX_STATE_BYTES = 1024 * 1024
# UTF-8正文经JSON控制字符转义最多膨胀六倍，仍需限制封存文件总大小。
_MAX_SEAL_BYTES = 6 * _MAX_PATCH_BYTES + 64 * 1024
_ERROR = "swebench_workspace_retirement_invalid"
_SHA256 = r"^[0-9a-f]{64}$"
_KEY_DOMAIN = b"harnessix.swebench.workspace-retirement.key/v1\x00"
_PAYLOAD_DOMAIN = b"harnessix.swebench.workspace-retirement.payload/v1\x00"


def _invalid() -> KernelError:
    # 错误只携带稳定分类，不泄露解析输入、路径、补丁正文或凭据。
    return KernelError(_ERROR, "SWE-bench 工作区封存证据、身份或权限无效")


class _RetirementContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class WorkspaceFileIdentity(_RetirementContract):
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    owner: int = Field(ge=0)
    mode: int = Field(ge=0, le=0o7777)


class SweBenchWorkspaceRetirementManifest(_RetirementContract):
    """认证的有界补丁全文，以及原状态、Native和工作区目录身份。"""

    spec_version: Literal["harnessix.swebench-workspace-retirement/v1"] = (
        "harnessix.swebench-workspace-retirement/v1"
    )
    plan_sha256: str = Field(pattern=_SHA256)
    config_fingerprint: str = Field(pattern=_SHA256)
    case_index: int = Field(ge=0, lt=SWE_BENCH_CASE_COUNT)
    instance_id: str = Field(max_length=256)
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    work_root: str = Field(max_length=4096)
    case_root: str = Field(max_length=4096)
    workspace_path: str = Field(max_length=4096)
    directory_identities: dict[str, WorkspaceFileIdentity]
    case_state_identity: WorkspaceFileIdentity
    session_identity: WorkspaceFileIdentity
    case_state_file_sha256: str = Field(pattern=_SHA256)
    session_sha256: str = Field(pattern=_SHA256)
    state_sha256: str = Field(pattern=_SHA256)
    run_id: UUID
    thread_id: UUID
    turn_id: UUID
    patch: str = Field(max_length=_MAX_PATCH_BYTES, repr=False)
    patch_sha256: str = Field(pattern=_SHA256)
    patch_bytes: int = Field(ge=0, le=_MAX_PATCH_BYTES)
    hmac_sha256: str = Field(pattern=_SHA256)


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _key(api_key: str) -> bytes:
    if type(api_key) is not str or not api_key:
        raise _invalid()
    return hmac.digest(api_key.encode("utf-8"), _KEY_DOMAIN, "sha256")


def _signature(payload: object, key: bytes) -> str:
    return hmac.new(key, _PAYLOAD_DOMAIN + _canonical(payload), hashlib.sha256).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise _invalid()
        value[name] = item
    return value


def _patch_bytes(patch: str) -> bytes:
    if type(patch) is not str:
        raise _invalid()
    body = patch.encode("utf-8", errors="strict")
    if len(body) > _MAX_PATCH_BYTES:
        raise _invalid()
    return body


def _seal_body(manifest: SweBenchWorkspaceRetirementManifest) -> bytes:
    return _canonical(manifest.model_dump(mode="json")) + b"\n"


def _marker_body(manifest: SweBenchWorkspaceRetirementManifest) -> bytes:
    return (
        _canonical(
            {
                "spec_version": "harnessix.swebench-retired-workspace/v1",
                "sealed_workspace_sha256": hashlib.sha256(_seal_body(manifest)).hexdigest(),
                "workspace_path": manifest.workspace_path,
                "workspace_identity": manifest.directory_identities[
                    manifest.workspace_path
                ].model_dump(),
            }
        )
        + b"\n"
    )
