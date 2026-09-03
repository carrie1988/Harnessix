from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from harnessix.domain.models import ApprovalDecision
from harnessix.patches.contracts import PatchManifest, patch_path
from harnessix.tools.contracts import ReadContract, Revision

MAX_COPY_FILES = 256
MAX_COPY_BYTES = 32 * 1024 * 1024
MAX_COPY_PLANS = 64
MAX_PLAN_BYTES = 32 * 1024 * 1024
PatchState = Literal[
    "pending",
    "approved",
    "rejected",
    "started",
    "applied",
    "failed",
    "uncertain",
    "observed_before",
    "observed_after",
    "diverged",
    "missing",
    "unavailable",
]


class PatchRecord(ReadContract):
    version: Literal["managed-patch-record/v1"] = "managed-patch-record/v1"
    plan_id: UUID
    workspace_id: UUID
    request_id: str = Field(min_length=1, max_length=128)
    manifest: PatchManifest
    approval_fingerprint: Revision
    state: PatchState
    decision: ApprovalDecision | None = None
    error_code: str | None = Field(default=None, max_length=128, pattern=r"^[a-z0-9_]+$")


class CopyFile(ReadContract):
    path: str = Field(min_length=1, max_length=1024)
    source_revision: Revision
    sha256: Revision
    size_bytes: int = Field(ge=0, le=1024 * 1024)
    mode: int = Field(ge=0, le=0o777)

    _path = field_validator("path")(patch_path)


class CopyManifest(ReadContract):
    version: Literal["managed-patch/v1"] = "managed-patch/v1"
    workspace_id: UUID
    source_scope: Revision
    workspace_scope: Revision
    files: tuple[CopyFile, ...] = Field(min_length=1, max_length=MAX_COPY_FILES)

    @model_validator(mode="after")
    def bounded_files(self) -> Self:
        paths = [entry.path for entry in self.files]
        if paths != sorted(set(paths)) or sum(e.size_bytes for e in self.files) > MAX_COPY_BYTES:
            raise ValueError("副本清单必须唯一有序且总量不超限")
        return self
