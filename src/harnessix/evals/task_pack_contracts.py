"""版本化 Coding Eval Task Pack、固定检查Profile与物化事实契约。"""

from __future__ import annotations

from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from harnessix.domain.models import ContractModel
from harnessix.evals.contracts import CodingEvalTask, EvalRepository
from harnessix.evals.suite_contracts import EvalTaskKind
from harnessix.tools.contracts import Revision
from harnessix.tools.workspace import digest

TaskPackLanguage = Literal["python", "javascript"]
TaskPackSourceKind = Literal["harnessix_authored", "third_party"]
ReviewFindingCategory = Literal["correctness", "security", "reliability", "maintainability"]
ReviewFindingSeverity = Literal["low", "medium", "high", "critical"]
GitObjectId = Annotated[str, Field(pattern=r"^[0-9a-f]{40,64}$")]

_IDENTIFIER = r"^[a-z][a-z0-9_-]{0,63}$"
_IMAGE_DIGEST = r"^[a-z0-9][a-z0-9._/-]{0,255}@sha256:[0-9a-f]{64}$"
_CONTAINER_PROGRAM = r"^/[A-Za-z0-9._/+:-]+(?:/[A-Za-z0-9._+:-]+)*$"
_SPDX_EXPRESSION = r"^[A-Za-z0-9.+() -]{1,128}$"


class TaskPackContract(ContractModel):
    """Task Pack公开合同的共同严格配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


def _relative_path(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or len(value.encode("utf-8")) > 1024
        or value.startswith(("/", "\\"))
        or "\\" in value
        or "\x00" in value
        or len(parts) > 64
        or any(
            part in {"", ".", ".."}
            or len(part.encode("utf-8")) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in parts
        )
    ):
        raise ValueError("Task Pack路径必须是规范POSIX相对路径")
    return value


def _single_line(value: str) -> str:
    if not value.strip() or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("Task Pack文本必须是非空单行")
    return value


class CodingEvalTaskPackLicense(TaskPackContract):
    """一个固定来源快照的许可证与权利链声明。"""

    source_kind: TaskPackSourceKind
    spdx_expression: str = Field(pattern=_SPDX_EXPRESSION)
    copyright_notice: str = Field(min_length=1, max_length=512)
    provenance_uri: str = Field(min_length=1, max_length=2048)
    license_file: str
    license_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime

    @field_validator("copyright_notice")
    @classmethod
    def valid_notice(cls, value: str) -> str:
        return _single_line(value)

    @field_validator("license_file")
    @classmethod
    def valid_license_file(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("provenance_uri")
    @classmethod
    def safe_provenance(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Task Pack来源URI包含控制字符")
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "urn"}:
            raise ValueError("Task Pack来源URI协议不受支持")
        if parsed.scheme == "https" and parsed.hostname is None:
            raise ValueError("Task Pack HTTPS来源URI缺少主机")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Task Pack来源URI不能内嵌凭据")
        return value


class CodingEvalTaskPackRepository(TaskPackContract):
    """Task Pack内一个不可变、可审计的来源仓库Archive。"""

    repository_id: str = Field(pattern=_IDENTIFIER)
    language: TaskPackLanguage
    repository: EvalRepository
    source_tree_oid: GitObjectId
    archive_file: str
    archive_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    archive_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    tracked_files: int = Field(ge=1, le=100_000)
    commit_created_at: AwareDatetime
    license: CodingEvalTaskPackLicense

    @field_validator("archive_file")
    @classmethod
    def valid_archive_file(cls, value: str) -> str:
        checked = _relative_path(value)
        if not checked.startswith("archives/") or not checked.endswith(".tar"):
            raise ValueError("Task Pack Archive必须位于archives目录且使用tar格式")
        return checked


class CodingEvalTaskPackProfile(TaskPackContract):
    """不含宿主Engine路径的固定无网Container检查Profile。"""

    profile_id: str = Field(pattern=_IDENTIFIER)
    version: str = Field(min_length=1, max_length=128)
    language: TaskPackLanguage
    description: str = Field(min_length=1, max_length=500)
    image: str = Field(pattern=_IMAGE_DIGEST)
    program: str = Field(pattern=_CONTAINER_PROGRAM)
    arguments: tuple[str, ...] = Field(default=(), max_length=128)
    timeout_seconds: int = Field(default=120, ge=1, le=3600, strict=True)
    max_output_bytes: int = Field(default=256 * 1024, ge=1024, le=8 * 1024 * 1024, strict=True)
    cpu_limit: float = Field(default=1.0, gt=0, le=8, strict=True)
    memory_bytes: int = Field(
        default=256 * 1024 * 1024,
        ge=16 * 1024 * 1024,
        le=8 * 1024 * 1024 * 1024,
        strict=True,
    )
    process_limit: int = Field(default=64, ge=1, le=512, strict=True)
    network_mode: Literal["none"] = "none"
    profile_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("version", "description")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return _single_line(value)

    @field_validator("arguments")
    @classmethod
    def valid_arguments(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        total = 0
        for value in values:
            if (
                type(value) is not str
                or not value
                or "\x00" in value
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("Task Pack Profile参数无效")
            total += len(value.encode("utf-8"))
        if total > 16 * 1024:
            raise ValueError("Task Pack Profile参数超过总大小上限")
        return values

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.profile_sha256 != coding_eval_task_pack_profile_digest(self):
            raise ValueError("Task Pack Profile摘要不一致")
        return self


def coding_eval_task_pack_profile_digest(profile: CodingEvalTaskPackProfile) -> str:
    return digest(profile.model_dump(mode="json", exclude={"profile_sha256"}, warnings="error"))


def build_coding_eval_task_pack_profile(
    *,
    profile_id: str,
    version: str,
    language: TaskPackLanguage,
    description: str,
    image: str,
    program: str,
    arguments: tuple[str, ...],
    timeout_seconds: int = 120,
    max_output_bytes: int = 256 * 1024,
    cpu_limit: float = 1.0,
    memory_bytes: int = 256 * 1024 * 1024,
    process_limit: int = 64,
) -> CodingEvalTaskPackProfile:
    """构造并自校验一个固定Container检查Profile。"""

    candidate = CodingEvalTaskPackProfile.model_construct(
        profile_id=profile_id,
        version=version,
        language=language,
        description=description,
        image=image,
        program=program,
        arguments=arguments,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        cpu_limit=cpu_limit,
        memory_bytes=memory_bytes,
        process_limit=process_limit,
        network_mode="none",
        profile_sha256="0" * 64,
    )
    return CodingEvalTaskPackProfile(
        **candidate.model_dump(exclude={"profile_sha256"}),
        profile_sha256=coding_eval_task_pack_profile_digest(candidate),
    )


class CodingEvalReviewFinding(TaskPackContract):
    """Review任务中一个必须识别且可定位的确定性Finding。"""

    finding_id: str = Field(pattern=_IDENTIFIER)
    category: ReviewFindingCategory
    severity: ReviewFindingSeverity
    path: str
    start_line: int = Field(ge=1, le=1_000_000)
    end_line: int = Field(ge=1, le=1_000_000)
    evidence_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _relative_path(value)

    @model_validator(mode="after")
    def line_range_is_valid(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("Review Finding行号范围无效")
        return self


class CodingEvalReviewOracle(TaskPackContract):
    """不依赖LLM Judge的版本化Review任务确定性Oracle。"""

    spec_version: Literal["harnessix.coding-eval-review-oracle/v1"] = (
        "harnessix.coding-eval-review-oracle/v1"
    )
    oracle_version: int = Field(ge=1)
    required_findings: tuple[CodingEvalReviewFinding, ...] = Field(min_length=1, max_length=32)
    oracle_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def findings_are_canonical(self) -> Self:
        identities = [item.finding_id for item in self.required_findings]
        if identities != sorted(set(identities)):
            raise ValueError("Review Finding必须按ID唯一排序")
        if self.oracle_sha256 != coding_eval_review_oracle_digest(self):
            raise ValueError("Review Oracle摘要不一致")
        return self


def coding_eval_review_oracle_digest(oracle: CodingEvalReviewOracle) -> str:
    return digest(oracle.model_dump(mode="json", exclude={"oracle_sha256"}, warnings="error"))


def build_coding_eval_review_oracle(
    *,
    oracle_version: int,
    required_findings: tuple[CodingEvalReviewFinding, ...],
) -> CodingEvalReviewOracle:
    """构造并自校验一个确定性Review Oracle。"""

    candidate = CodingEvalReviewOracle.model_construct(
        oracle_version=oracle_version,
        required_findings=required_findings,
        oracle_sha256="0" * 64,
    )
    return CodingEvalReviewOracle(
        **candidate.model_dump(exclude={"oracle_sha256"}),
        oracle_sha256=coding_eval_review_oracle_digest(candidate),
    )


class CodingEvalTaskPackCase(TaskPackContract):
    """Task Pack中任务、类别、仓库、检查Profile和Review Oracle的绑定。"""

    case_id: str = Field(pattern=_IDENTIFIER)
    task_kind: EvalTaskKind
    repository_id: str = Field(pattern=_IDENTIFIER)
    profile_id: str = Field(pattern=_IDENTIFIER)
    task: CodingEvalTask
    review_oracle: CodingEvalReviewOracle | None = None

    @model_validator(mode="after")
    def review_contract_is_explicit(self) -> Self:
        if (self.task_kind == "review") != (self.review_oracle is not None):
            raise ValueError("Review任务必须且只能绑定Review Oracle")
        return self


class CodingEvalTaskPack(TaskPackContract):
    """可发布、可复核且禁止运行时命令注入的Task Pack v1。"""

    spec_version: Literal["harnessix.coding-eval-task-pack/v1"] = (
        "harnessix.coding-eval-task-pack/v1"
    )
    pack_id: str = Field(pattern=_IDENTIFIER)
    pack_version: int = Field(ge=1)
    repositories: tuple[CodingEvalTaskPackRepository, ...] = Field(min_length=2, max_length=20)
    profiles: tuple[CodingEvalTaskPackProfile, ...] = Field(min_length=2, max_length=32)
    cases: tuple[CodingEvalTaskPackCase, ...] = Field(min_length=2, max_length=50)
    created_at: AwareDatetime
    pack_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def pack_is_canonical_and_bound(self) -> Self:
        _require_sorted_unique(self.repositories, "repository_id", "Task Pack仓库")
        _require_sorted_unique(self.profiles, "profile_id", "Task Pack Profile")
        _require_sorted_unique(self.cases, "case_id", "Task Pack Case")
        if len({item.language for item in self.repositories}) < 2:
            raise ValueError("Task Pack必须覆盖至少两种编程语言")
        repositories = {item.repository_id: item for item in self.repositories}
        profiles = {item.profile_id: item for item in self.profiles}
        task_identities: set[tuple[str, int]] = set()
        for case in self.cases:
            repository = repositories.get(case.repository_id)
            profile = profiles.get(case.profile_id)
            identity = (case.task.task_id, case.task.task_version)
            if identity in task_identities:
                raise ValueError("Task Pack任务ID与版本必须唯一")
            task_identities.add(identity)
            if repository is None or profile is None:
                raise ValueError("Task Pack Case引用不存在的仓库或Profile")
            if case.task.repository != repository.repository:
                raise ValueError("Task Pack Case任务仓库身份不一致")
            if profile.language != repository.language:
                raise ValueError("Task Pack Profile语言与仓库不一致")
            if case.task.required_test_profiles != (profile.profile_id,):
                raise ValueError("Task Pack任务必须精确绑定唯一固定Profile")
        if self.pack_sha256 != coding_eval_task_pack_digest(self):
            raise ValueError("Task Pack摘要不一致")
        return self

    def repository(self, repository_id: str) -> CodingEvalTaskPackRepository:
        return _lookup(self.repositories, "repository_id", repository_id, "Task Pack仓库不存在")

    def profile(self, profile_id: str) -> CodingEvalTaskPackProfile:
        return _lookup(self.profiles, "profile_id", profile_id, "Task Pack Profile不存在")

    def case(self, case_id: str) -> CodingEvalTaskPackCase:
        return _lookup(self.cases, "case_id", case_id, "Task Pack Case不存在")


def _require_sorted_unique(values: tuple[object, ...], field: str, label: str) -> None:
    identities = [getattr(item, field) for item in values]
    if identities != sorted(set(identities)):
        raise ValueError(f"{label}必须按ID唯一排序")


def _lookup[T](values: tuple[T, ...], field: str, identity: str, message: str) -> T:
    for item in values:
        if getattr(item, field) == identity:
            return item
    raise KeyError(message)


def coding_eval_task_pack_digest(pack: CodingEvalTaskPack) -> str:
    return digest(pack.model_dump(mode="json", exclude={"pack_sha256"}, warnings="error"))


def build_coding_eval_task_pack(
    *,
    pack_id: str,
    pack_version: int,
    repositories: tuple[CodingEvalTaskPackRepository, ...],
    profiles: tuple[CodingEvalTaskPackProfile, ...],
    cases: tuple[CodingEvalTaskPackCase, ...],
    created_at: AwareDatetime,
) -> CodingEvalTaskPack:
    """从已评审组成项构造并冻结Task Pack摘要。"""

    candidate = CodingEvalTaskPack.model_construct(
        pack_id=pack_id,
        pack_version=pack_version,
        repositories=repositories,
        profiles=profiles,
        cases=cases,
        created_at=created_at,
        pack_sha256="0" * 64,
    )
    return CodingEvalTaskPack(
        **candidate.model_dump(exclude={"pack_sha256"}),
        pack_sha256=coding_eval_task_pack_digest(candidate),
    )


class CodingEvalTaskPackMaterialization(TaskPackContract):
    """一个Task Pack Case私有工作区的可恢复物化身份。"""

    spec_version: Literal["harnessix.coding-eval-task-pack-materialization/v1"] = (
        "harnessix.coding-eval-task-pack-materialization/v1"
    )
    run_id: UUID
    pack_id: str = Field(pattern=_IDENTIFIER)
    pack_version: int = Field(ge=1)
    pack_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    case_id: str = Field(pattern=_IDENTIFIER)
    task_id: str = Field(min_length=1, max_length=128)
    task_version: int = Field(ge=1)
    task_fingerprint: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    repository_id: str = Field(pattern=_IDENTIFIER)
    source_revision: GitObjectId
    source_tree_oid: GitObjectId
    source_archive_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_revision: GitObjectId
    baseline_tree_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    tracked_files: int = Field(ge=1, le=100_000)
    archive_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    workspace_directory: Literal["workspace"] = "workspace"
    status: Literal["ready"] = "ready"
    created_at: AwareDatetime
