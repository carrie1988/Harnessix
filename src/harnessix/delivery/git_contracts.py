from __future__ import annotations

import ntpath
import posixpath
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from harnessix.delivery.contracts import DeliveryContract
from harnessix.execution.contracts import canonical_digest
from harnessix.tools.contracts import Revision
from harnessix.workspace.contracts import PlatformKind

GitObjectId = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
GitWorktreeState = Literal["prepared", "creating", "ready", "diverged", "unknown"]
GitCommitState = Literal[
    "prepared", "committing", "interrupted", "committed", "diverged", "unknown"
]


class GitRepositoryBinding(DeliveryContract):
    spec_version: Literal["harnessix.git-repository-binding/v1"] = (
        "harnessix.git-repository-binding/v1"
    )
    platform: PlatformKind
    workspace_id: Revision
    root_path_sha256: Revision
    root_identity: Revision
    common_directory_sha256: Revision
    common_directory_identity: Revision
    head_oid: GitObjectId
    head_tree_oid: GitObjectId
    object_format: Literal["sha1", "sha256"]
    status_sha256: Revision
    config_sha256: Revision
    git_executable_identity: Revision
    git_version: str = Field(min_length=1, max_length=128)
    implementation_digest: Revision
    digest: Revision

    @model_validator(mode="after")
    def complete_binding(self) -> Self:
        expected_length = 40 if self.object_format == "sha1" else 64
        if len(self.head_oid) != expected_length or len(self.head_tree_oid) != expected_length:
            raise ValueError("Git对象格式与OID长度不一致")
        if self.digest != git_repository_binding_digest(self):
            raise ValueError("Git Repository Binding摘要不一致")
        return self


def git_repository_binding_digest(binding: GitRepositoryBinding) -> str:
    return canonical_digest(binding.model_dump(mode="json", exclude={"digest"}, warnings="error"))


class ManagedGitWorktreePlan(DeliveryContract):
    spec_version: Literal["harnessix.managed-git-worktree-plan/v1"] = (
        "harnessix.managed-git-worktree-plan/v1"
    )
    worktree_id: UUID
    transaction_id: UUID
    transaction_plan_fingerprint: Revision
    repository: GitRepositoryBinding
    path: str = Field(min_length=1, max_length=4096)
    created_at: AwareDatetime
    fingerprint: Revision

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if "\0" in value or any(ord(character) < 32 for character in value):
            raise ValueError("受管Git worktree路径无效")
        return value

    @model_validator(mode="after")
    def complete_plan(self) -> Self:
        absolute = (
            ntpath.isabs(self.path)
            if self.repository.platform == "windows"
            else posixpath.isabs(self.path)
        )
        if not absolute or self.fingerprint != managed_git_worktree_plan_fingerprint(self):
            raise ValueError("受管Git Worktree Plan绑定无效")
        return self


def managed_git_worktree_plan_fingerprint(plan: ManagedGitWorktreePlan) -> str:
    return canonical_digest(plan.model_dump(mode="json", exclude={"fingerprint"}, warnings="error"))


class ManagedGitWorktreeBinding(DeliveryContract):
    spec_version: Literal["harnessix.managed-git-worktree-binding/v1"] = (
        "harnessix.managed-git-worktree-binding/v1"
    )
    worktree_id: UUID
    plan_fingerprint: Revision
    path_identity: Revision
    gitfile_sha256: Revision
    admin_directory_sha256: Revision
    admin_directory_identity: Revision
    backlink_sha256: Revision
    head_oid: GitObjectId
    digest: Revision

    @model_validator(mode="after")
    def complete_binding(self) -> Self:
        if self.digest != managed_git_worktree_binding_digest(self):
            raise ValueError("受管Git Worktree Binding摘要不一致")
        return self


def managed_git_worktree_binding_digest(binding: ManagedGitWorktreeBinding) -> str:
    return canonical_digest(binding.model_dump(mode="json", exclude={"digest"}, warnings="error"))


class ManagedGitWorktreeRecord(DeliveryContract):
    spec_version: Literal["harnessix.managed-git-worktree-record/v1"] = (
        "harnessix.managed-git-worktree-record/v1"
    )
    worktree_id: UUID
    plan: ManagedGitWorktreePlan
    state: GitWorktreeState
    sequence: int = Field(ge=0, le=64)
    binding: ManagedGitWorktreeBinding | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    record_digest: Revision

    @model_validator(mode="after")
    def complete_record(self) -> Self:
        if self.worktree_id != self.plan.worktree_id:
            raise ValueError("Git Worktree Record身份不一致")
        if self.state == "prepared":
            valid = self.sequence == 0 and self.binding is None and self.started_at is None
        elif self.state == "creating":
            valid = (
                self.sequence > 0
                and self.binding is None
                and self.started_at is not None
                and self.finished_at is None
            )
        elif self.state == "ready":
            valid = (
                self.binding is not None
                and self.started_at is not None
                and self.finished_at is not None
            )
        else:
            valid = (
                self.binding is None
                and self.started_at is not None
                and self.finished_at is not None
                and self.error_code is not None
            )
        if (
            not valid
            or (self.binding is not None and self.binding.worktree_id != self.worktree_id)
            or (self.state not in {"diverged", "unknown"} and self.error_code is not None)
        ):
            raise ValueError("Git Worktree Record状态无效")
        if self.record_digest != managed_git_worktree_record_digest(self):
            raise ValueError("Git Worktree Record摘要不一致")
        return self


def managed_git_worktree_record_digest(record: ManagedGitWorktreeRecord) -> str:
    return canonical_digest(
        record.model_dump(mode="json", exclude={"record_digest"}, warnings="error")
    )


class GitCheckpoint(DeliveryContract):
    spec_version: Literal["harnessix.git-checkpoint/v1"] = "harnessix.git-checkpoint/v1"
    checkpoint_id: UUID
    worktree_id: UUID
    worktree_plan_fingerprint: Revision
    worktree_binding_digest: Revision
    transaction_id: UUID
    transaction_plan_fingerprint: Revision
    repository_binding_digest: Revision
    base_commit_oid: GitObjectId
    base_tree_oid: GitObjectId
    tree_oid: GitObjectId
    mutations_digest: Revision
    created_at: AwareDatetime
    digest: Revision

    @model_validator(mode="after")
    def complete_checkpoint(self) -> Self:
        if len(
            {len(self.base_commit_oid), len(self.base_tree_oid), len(self.tree_oid)}
        ) != 1 or self.digest != git_checkpoint_digest(self):
            raise ValueError("Git Checkpoint摘要不一致")
        return self


def git_checkpoint_digest(checkpoint: GitCheckpoint) -> str:
    return canonical_digest(
        checkpoint.model_dump(mode="json", exclude={"digest"}, warnings="error")
    )


class GitCommitSpec(DeliveryContract):
    spec_version: Literal["harnessix.git-commit-spec/v1"] = "harnessix.git-commit-spec/v1"
    commit_id: UUID
    checkpoint: GitCheckpoint
    branch_ref: str = Field(min_length=12, max_length=1024)
    parent_oid: GitObjectId
    tree_oid: GitObjectId
    author_name: str = Field(min_length=1, max_length=256)
    author_email: str = Field(min_length=3, max_length=320)
    message: str = Field(min_length=1, max_length=65536, repr=False)
    authored_at: AwareDatetime
    hooks_policy: Literal["disabled"] = "disabled"
    raw_commit_sha256: Revision
    expected_commit_oid: GitObjectId
    implementation_digest: Revision
    fingerprint: Revision

    @model_validator(mode="after")
    def complete_spec(self) -> Self:
        invalid_identity = any(character in self.author_name for character in "\n\r<>") or any(
            character in self.author_email for character in "\n\r<> \t"
        )
        if (
            not self.branch_ref.startswith("refs/heads/")
            or any(ord(character) < 32 or character in " ~^:?*[\\" for character in self.branch_ref)
            or self.branch_ref.endswith(("/", ".", ".lock"))
            or ".." in self.branch_ref
            or "@{" in self.branch_ref
            or invalid_identity
            or "@" not in self.author_email
            or "\0" in self.message
            or not self.message.endswith("\n")
            or self.parent_oid != self.checkpoint.base_commit_oid
            or self.tree_oid != self.checkpoint.tree_oid
            or len(self.expected_commit_oid) != len(self.parent_oid)
            or self.fingerprint != git_commit_spec_fingerprint(self)
        ):
            raise ValueError("Git Commit Spec绑定无效")
        return self


def git_commit_spec_fingerprint(spec: GitCommitSpec) -> str:
    return canonical_digest(spec.model_dump(mode="json", exclude={"fingerprint"}, warnings="error"))


class GitCommitRecord(DeliveryContract):
    spec_version: Literal["harnessix.git-commit-record/v1"] = "harnessix.git-commit-record/v1"
    commit_id: UUID
    spec: GitCommitSpec
    state: GitCommitState
    sequence: int = Field(ge=0, le=64)
    commit_oid: GitObjectId | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    record_digest: Revision

    @model_validator(mode="after")
    def complete_record(self) -> Self:
        if self.commit_id != self.spec.commit_id:
            raise ValueError("Git Commit Record身份不一致")
        if self.state == "prepared":
            valid = self.sequence == 0 and self.commit_oid is None and self.started_at is None
        elif self.state in {"committing", "interrupted"}:
            valid = (
                self.commit_oid is None and self.started_at is not None and self.finished_at is None
            )
        elif self.state == "committed":
            valid = (
                self.commit_oid == self.spec.expected_commit_oid
                and self.started_at is not None
                and self.finished_at is not None
            )
        else:
            valid = (
                self.commit_oid is None
                and self.started_at is not None
                and self.finished_at is not None
                and self.error_code is not None
            )
        if (
            not valid
            or (self.state not in {"diverged", "unknown"} and self.error_code is not None)
            or self.record_digest != git_commit_record_digest(self)
        ):
            raise ValueError("Git Commit Record状态或摘要无效")
        return self


def git_commit_record_digest(record: GitCommitRecord) -> str:
    return canonical_digest(
        record.model_dump(mode="json", exclude={"record_digest"}, warnings="error")
    )


def _worktree_record(
    plan: ManagedGitWorktreePlan,
    state: GitWorktreeState,
    sequence: int,
    binding: ManagedGitWorktreeBinding | None,
    started_at: datetime | None,
    finished_at: datetime | None,
    error_code: str | None,
) -> ManagedGitWorktreeRecord:
    candidate = ManagedGitWorktreeRecord.model_construct(
        _fields_set=None,
        worktree_id=plan.worktree_id,
        plan=plan,
        state=state,
        sequence=sequence,
        binding=binding,
        started_at=started_at,
        finished_at=finished_at,
        error_code=error_code,
        record_digest="0" * 64,
    )
    return ManagedGitWorktreeRecord(
        worktree_id=plan.worktree_id,
        plan=plan,
        state=state,
        sequence=sequence,
        binding=binding,
        started_at=started_at,
        finished_at=finished_at,
        error_code=error_code,
        record_digest=managed_git_worktree_record_digest(candidate),
    )


def new_worktree_record(plan: ManagedGitWorktreePlan) -> ManagedGitWorktreeRecord:
    return _worktree_record(plan, "prepared", 0, None, None, None, None)


def transition_worktree_record(
    record: ManagedGitWorktreeRecord,
    *,
    state: GitWorktreeState,
    now: datetime,
    binding: ManagedGitWorktreeBinding | None = None,
    error_code: str | None = None,
) -> ManagedGitWorktreeRecord:
    return _worktree_record(
        record.plan,
        state,
        record.sequence + 1,
        binding,
        record.started_at or now,
        now if state in {"ready", "diverged", "unknown"} else None,
        error_code,
    )


def _commit_record(
    spec: GitCommitSpec,
    state: GitCommitState,
    sequence: int,
    commit_oid: str | None,
    started_at: datetime | None,
    finished_at: datetime | None,
    error_code: str | None,
) -> GitCommitRecord:
    candidate = GitCommitRecord.model_construct(
        _fields_set=None,
        commit_id=spec.commit_id,
        spec=spec,
        state=state,
        sequence=sequence,
        commit_oid=commit_oid,
        started_at=started_at,
        finished_at=finished_at,
        error_code=error_code,
        record_digest="0" * 64,
    )
    return GitCommitRecord(
        commit_id=spec.commit_id,
        spec=spec,
        state=state,
        sequence=sequence,
        commit_oid=commit_oid,
        started_at=started_at,
        finished_at=finished_at,
        error_code=error_code,
        record_digest=git_commit_record_digest(candidate),
    )


def new_commit_record(spec: GitCommitSpec) -> GitCommitRecord:
    return _commit_record(spec, "prepared", 0, None, None, None, None)


def transition_commit_record(
    record: GitCommitRecord,
    *,
    state: GitCommitState,
    now: datetime,
    commit_oid: str | None = None,
    error_code: str | None = None,
) -> GitCommitRecord:
    return _commit_record(
        record.spec,
        state,
        record.sequence + 1,
        commit_oid,
        record.started_at or now,
        now if state in {"committed", "diverged", "unknown"} else None,
        error_code,
    )
