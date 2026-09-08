from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from harnessix.domain.models import ContractModel
from harnessix.execution.contracts import canonical_digest
from harnessix.tools.contracts import Revision
from harnessix.workspace.contracts import WorkspaceSnapshot
from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key

MAX_TRANSACTION_FILES = 256
MAX_TRANSACTION_FILE_BYTES = 8 * 1024 * 1024
MAX_TRANSACTION_IMAGE_BYTES = 32 * 1024 * 1024
PROTECTED_COMPONENTS = frozenset(
    {".agents", ".codex", ".git", ".gitattributes", ".gitmodules", ".harnessix", ".lfsconfig"}
)

FilePresence = Literal["absent", "file"]
FileMode = Literal[420, 493]
TransactionState = Literal[
    "prepared",
    "publishing",
    "interrupted",
    "published",
    "diverged",
    "unknown",
]
DiffKind = Literal["added", "modified", "deleted", "renamed"]


class DeliveryContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class WorkspaceFileVersion(DeliveryContract):
    presence: FilePresence
    sha256: Revision | None = None
    size: int = Field(ge=0, le=MAX_TRANSACTION_FILE_BYTES)
    mode: FileMode | None = None

    @model_validator(mode="after")
    def complete_version(self) -> Self:
        if self.presence == "absent":
            if self.sha256 is not None or self.size != 0 or self.mode is not None:
                raise ValueError("缺失文件不能携带内容或模式")
        elif self.sha256 is None or self.mode is None:
            raise ValueError("普通文件必须携带内容摘要和模式")
        return self


class WorkspaceMutation(DeliveryContract):
    path: str = Field(min_length=1, max_length=4096)
    before: WorkspaceFileVersion
    after: WorkspaceFileVersion

    @model_validator(mode="after")
    def has_effect(self) -> Self:
        if self.before == self.after:
            raise ValueError("Workspace Mutation必须产生变化")
        return self


class WorkspaceTransactionPlan(DeliveryContract):
    spec_version: Literal["harnessix.workspace-transaction-plan/v1"] = (
        "harnessix.workspace-transaction-plan/v1"
    )
    transaction_id: UUID
    request_id: str = Field(min_length=1, max_length=128)
    source: WorkspaceSnapshot
    mutations: tuple[WorkspaceMutation, ...] = Field(min_length=1, max_length=MAX_TRANSACTION_FILES)
    created_at: AwareDatetime
    fingerprint: Revision

    @model_validator(mode="after")
    def complete_plan(self) -> Self:
        normalized = [
            normalize_workspace_path(item.path, self.source.platform) for item in self.mutations
        ]
        keys = [path_comparison_key(path, self.source.platform) for path in normalized]
        if (
            any(
                item.path != path or path == "."
                for item, path in zip(self.mutations, normalized, strict=True)
            )
            or keys != sorted(keys)
            or len(keys) != len(set(keys))
            or any(
                component.casefold() in PROTECTED_COMPONENTS
                or component.casefold().startswith(".env")
                for path in normalized
                for component in path.split("/")
            )
            or sum(item.before.size + item.after.size for item in self.mutations)
            > MAX_TRANSACTION_IMAGE_BYTES
        ):
            raise ValueError("Workspace Transaction路径、顺序或容量无效")
        if self.fingerprint != workspace_transaction_plan_fingerprint(self):
            raise ValueError("Workspace Transaction Plan指纹不一致")
        return self


def workspace_transaction_plan_fingerprint(plan: WorkspaceTransactionPlan) -> str:
    return canonical_digest(plan.model_dump(mode="json", exclude={"fingerprint"}, warnings="error"))


class WorkspaceTransactionRecord(DeliveryContract):
    spec_version: Literal["harnessix.workspace-transaction-record/v1"] = (
        "harnessix.workspace-transaction-record/v1"
    )
    transaction_id: UUID
    plan: WorkspaceTransactionPlan
    state: TransactionState
    sequence: int = Field(ge=0, le=4096)
    cursor: int = Field(ge=0, le=MAX_TRANSACTION_FILES)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    record_digest: Revision

    @model_validator(mode="after")
    def complete_record(self) -> Self:
        count = len(self.plan.mutations)
        if self.transaction_id != self.plan.transaction_id or self.cursor > count:
            raise ValueError("Workspace Transaction Record绑定无效")
        if self.state == "prepared":
            valid = self.sequence == 0 and self.cursor == 0 and self.started_at is None
        elif self.state in {"publishing", "interrupted"}:
            valid = self.sequence > 0 and self.started_at is not None and self.finished_at is None
        elif self.state == "published":
            valid = (
                self.cursor == count
                and self.started_at is not None
                and self.finished_at is not None
            )
        else:
            valid = self.started_at is not None and self.finished_at is not None
        if not valid or (
            self.finished_at is not None
            and (self.started_at is None or self.finished_at < self.started_at)
        ):
            raise ValueError("Workspace Transaction Record状态字段无效")
        if self.record_digest != workspace_transaction_record_digest(self):
            raise ValueError("Workspace Transaction Record摘要不一致")
        return self


def workspace_transaction_record_digest(record: WorkspaceTransactionRecord) -> str:
    return canonical_digest(
        record.model_dump(mode="json", exclude={"record_digest"}, warnings="error")
    )


def new_transaction_record(plan: WorkspaceTransactionPlan) -> WorkspaceTransactionRecord:
    candidate = WorkspaceTransactionRecord.model_construct(
        _fields_set=None,
        transaction_id=plan.transaction_id,
        plan=plan,
        state="prepared",
        sequence=0,
        cursor=0,
        started_at=None,
        finished_at=None,
        error_code=None,
        record_digest="0" * 64,
    )
    return WorkspaceTransactionRecord(
        transaction_id=plan.transaction_id,
        plan=plan,
        state="prepared",
        sequence=0,
        cursor=0,
        started_at=None,
        finished_at=None,
        error_code=None,
        record_digest=workspace_transaction_record_digest(candidate),
    )


def transition_transaction_record(
    record: WorkspaceTransactionRecord,
    *,
    state: TransactionState,
    cursor: int,
    now: datetime,
    error_code: str | None = None,
) -> WorkspaceTransactionRecord:
    started = record.started_at or now
    finished = now if state in {"published", "diverged", "unknown"} else None
    candidate = WorkspaceTransactionRecord.model_construct(
        _fields_set=None,
        transaction_id=record.transaction_id,
        plan=record.plan,
        state=state,
        sequence=record.sequence + 1,
        cursor=cursor,
        started_at=started,
        finished_at=finished,
        error_code=error_code,
        record_digest="0" * 64,
    )
    return WorkspaceTransactionRecord(
        transaction_id=record.transaction_id,
        plan=record.plan,
        state=state,
        sequence=record.sequence + 1,
        cursor=cursor,
        started_at=started,
        finished_at=finished,
        error_code=error_code,
        record_digest=workspace_transaction_record_digest(candidate),
    )


class WorkspaceDiffEntry(DeliveryContract):
    kind: DiffKind
    path: str = Field(min_length=1, max_length=4096)
    original_path: str | None = Field(default=None, min_length=1, max_length=4096)
    before: WorkspaceFileVersion
    after: WorkspaceFileVersion
    binary: bool

    @model_validator(mode="after")
    def complete_entry(self) -> Self:
        renamed = self.kind == "renamed"
        if renamed != (self.original_path is not None):
            raise ValueError("只有重命名Diff携带原路径")
        if renamed and (
            self.before.presence != "file"
            or self.after.presence != "file"
            or self.before != self.after
            or self.original_path == self.path
        ):
            raise ValueError("重命名Diff必须绑定相同文件版本的两个路径")
        if self.kind == "added" and not (
            self.before.presence == "absent" and self.after.presence == "file"
        ):
            raise ValueError("新增Diff状态无效")
        if self.kind == "deleted" and not (
            self.before.presence == "file" and self.after.presence == "absent"
        ):
            raise ValueError("删除Diff状态无效")
        if self.kind == "modified" and not (self.before.presence == self.after.presence == "file"):
            raise ValueError("修改Diff状态无效")
        return self


class WorkspaceDiffDocument(DeliveryContract):
    spec_version: Literal["harnessix.workspace-diff/v1"] = "harnessix.workspace-diff/v1"
    transaction_id: UUID
    plan_fingerprint: Revision
    entries: tuple[WorkspaceDiffEntry, ...] = Field(min_length=1, max_length=MAX_TRANSACTION_FILES)
    text: str = Field(repr=False)
    utf8_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    sha256: Revision

    @model_validator(mode="after")
    def complete_document(self) -> Self:
        import hashlib

        try:
            body = self.text.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ValueError("Workspace Diff不是合法UTF-8") from None
        if len(body) != self.utf8_bytes or hashlib.sha256(body).hexdigest() != self.sha256:
            raise ValueError("Workspace Diff正文与摘要不一致")
        return self
