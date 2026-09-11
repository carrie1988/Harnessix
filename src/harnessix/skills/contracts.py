"""可信Skill扩展：定义版本化数据合同及其跨字段一致性校验。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from harnessix.execution.contracts import ExecutionContract, canonical_digest
from harnessix.tools.contracts import Revision

SkillSourceKind = Literal["bundled", "user", "workspace"]
SkillAccessOperation = Literal["load", "read_resource"]
SkillAccessOutcome = Literal["succeeded", "failed"]


def _valid_relative_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Skill路径必须是规范相对路径")
    return value


class SkillSourceSnapshot(ExecutionContract):
    spec_version: Literal["harnessix.skill-source-snapshot/v1"] = (
        "harnessix.skill-source-snapshot/v1"
    )
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    kind: SkillSourceKind
    root_sha256: Revision
    source_revision: Revision
    skill_count: int = Field(ge=0, le=2048)
    source_sha256: Revision

    @model_validator(mode="after")
    def valid_digest(self) -> Self:
        if self.source_sha256 != skill_source_snapshot_digest(self):
            raise ValueError("Skill来源快照摘要不一致")
        return self


def skill_source_snapshot_digest(source: SkillSourceSnapshot) -> str:
    return canonical_digest(source.model_dump(mode="json", exclude={"source_sha256"}))


class SkillManifestSnapshot(ExecutionContract):
    spec_version: Literal["harnessix.skill-manifest-snapshot/v1"] = (
        "harnessix.skill-manifest-snapshot/v1"
    )
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    source_kind: SkillSourceKind
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    qualified_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}/[a-z0-9][a-z0-9._-]{0,63}$")
    description: str = Field(min_length=1, max_length=1024)
    declared_version: str | None = Field(default=None, min_length=1, max_length=128)
    effective_version: str = Field(min_length=1, max_length=128)
    relative_path: str = Field(min_length=8, max_length=4096)
    content_utf8_bytes: int = Field(ge=1, le=262_144)
    content_sha256: Revision
    metadata_sha256: Revision
    manifest_sha256: Revision

    @field_validator("relative_path")
    @classmethod
    def valid_relative_path(cls, value: str) -> str:
        value = _valid_relative_path(value)
        if value.split("/")[-1] != "SKILL.md":
            raise ValueError("Skill清单文件必须命名为SKILL.md")
        return value

    @field_validator("description", "declared_version", "effective_version")
    @classmethod
    def single_line(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip() or "\x00" in value or "\n" in value or "\r" in value
        ):
            raise ValueError("Skill元数据必须是非空单行文本")
        return value

    @model_validator(mode="after")
    def valid_identity(self) -> Self:
        if self.qualified_name != f"{self.source_id}/{self.name}":
            raise ValueError("Skill限定名称与来源不一致")
        if self.effective_version != (
            self.declared_version or f"sha256:{self.content_sha256[:16]}"
        ):
            raise ValueError("Skill有效版本不一致")
        if self.manifest_sha256 != skill_manifest_snapshot_digest(self):
            raise ValueError("Skill清单摘要不一致")
        return self


def skill_manifest_snapshot_digest(skill: SkillManifestSnapshot) -> str:
    return canonical_digest(skill.model_dump(mode="json", exclude={"manifest_sha256"}))


class SkillDiscoveryIssue(ExecutionContract):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    code: str = Field(pattern=r"^skill_[a-z0-9_]{1,119}$")
    path_sha256: Revision


class SkillNameConflict(ExecutionContract):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    qualified_names: tuple[str, ...] = Field(min_length=2, max_length=64)

    @model_validator(mode="after")
    def canonical_names(self) -> Self:
        if list(self.qualified_names) != sorted(self.qualified_names) or len(
            set(self.qualified_names)
        ) != len(self.qualified_names):
            raise ValueError("Skill冲突名称必须规范排序且唯一")
        return self


class SkillCatalogSnapshot(ExecutionContract):
    spec_version: Literal["harnessix.skill-catalog-snapshot/v1"] = (
        "harnessix.skill-catalog-snapshot/v1"
    )
    catalog_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    generation: int = Field(ge=1)
    captured_at: AwareDatetime
    sources: tuple[SkillSourceSnapshot, ...] = Field(max_length=32)
    skills: tuple[SkillManifestSnapshot, ...] = Field(max_length=2048)
    conflicts: tuple[SkillNameConflict, ...] = Field(max_length=512)
    issues: tuple[SkillDiscoveryIssue, ...] = Field(max_length=2048)
    catalog_sha256: Revision

    @model_validator(mode="after")
    def canonical_catalog(self) -> Self:
        source_ids = [item.source_id for item in self.sources]
        qualified = [item.qualified_name for item in self.skills]
        issues = [(item.source_id, item.path_sha256, item.code) for item in self.issues]
        if (
            source_ids != sorted(source_ids)
            or len(set(source_ids)) != len(source_ids)
            or qualified != sorted(qualified)
            or len(set(qualified)) != len(qualified)
            or [item.name for item in self.conflicts]
            != sorted(item.name for item in self.conflicts)
            or issues != sorted(issues)
            or any(item.source_id not in source_ids for item in self.skills)
            or self.catalog_sha256 != skill_catalog_snapshot_digest(self)
        ):
            raise ValueError("Skill目录快照不规范")
        expected_conflicts = {
            name: tuple(sorted(item.qualified_name for item in self.skills if item.name == name))
            for name in {item.name for item in self.skills}
            if sum(item.name == name for item in self.skills) > 1
        }
        if {item.name: item.qualified_names for item in self.conflicts} != expected_conflicts:
            raise ValueError("Skill同名冲突索引不一致")
        return self


def skill_catalog_snapshot_digest(catalog: SkillCatalogSnapshot) -> str:
    return canonical_digest(
        catalog.model_dump(
            mode="json",
            exclude={"generation", "captured_at", "catalog_sha256"},
            warnings="error",
        )
    )


class SkillLoadInput(ExecutionContract):
    catalog_sha256: Revision
    name: str = Field(min_length=1, max_length=128)
    expected_manifest_sha256: Revision


class SkillResourceReadInput(SkillLoadInput):
    path: str = Field(min_length=1, max_length=4096)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _valid_relative_path(value)


class SkillContent(ExecutionContract):
    spec_version: Literal["harnessix.skill-content/v1"] = "harnessix.skill-content/v1"
    catalog_sha256: Revision
    manifest_sha256: Revision
    qualified_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}/[a-z0-9][a-z0-9._-]{0,63}$")
    effective_version: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=262_144)
    content_sha256: Revision
    resources: tuple[str, ...] = Field(max_length=64)

    @field_validator("resources")
    @classmethod
    def canonical_resources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if list(value) != sorted(value) or len(set(value)) != len(value):
            raise ValueError("Skill资源路径必须规范排序且唯一")
        for path in value:
            _valid_relative_path(path)
        return value

    @model_validator(mode="after")
    def content_matches_digest(self) -> Self:
        if (
            len(self.content.encode()) > 262_144
            or hashlib.sha256(self.content.encode()).hexdigest() != self.content_sha256
        ):
            raise ValueError("Skill正文摘要不一致")
        return self


class SkillResourceContent(ExecutionContract):
    spec_version: Literal["harnessix.skill-resource-content/v1"] = (
        "harnessix.skill-resource-content/v1"
    )
    catalog_sha256: Revision
    manifest_sha256: Revision
    qualified_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}/[a-z0-9][a-z0-9._-]{0,63}$")
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=65_536)
    content_sha256: Revision

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _valid_relative_path(value)

    @model_validator(mode="after")
    def content_matches_digest(self) -> Self:
        if (
            len(self.content.encode()) > 65_536
            or hashlib.sha256(self.content.encode()).hexdigest() != self.content_sha256
        ):
            raise ValueError("Skill资源摘要不一致")
        return self


class SkillAccessEvent(ExecutionContract):
    spec_version: Literal["harnessix.skill-access-event/v1"] = "harnessix.skill-access-event/v1"
    catalog_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    sequence: int = Field(ge=1)
    generation: int = Field(ge=1)
    catalog_sha256: Revision
    operation: SkillAccessOperation
    manifest_sha256: Revision
    resource_path_sha256: Revision | None = None
    outcome: SkillAccessOutcome
    result_sha256: Revision | None = None
    error_code: str | None = Field(default=None, pattern=r"^skill_[a-z0-9_]{1,119}$")
    occurred_at: AwareDatetime
    previous_digest: Revision | None = None
    digest: Revision

    @model_validator(mode="after")
    def valid_event(self) -> Self:
        if (self.sequence == 1) != (self.previous_digest is None):
            raise ValueError("Skill访问事件前序摘要不一致")
        if (self.operation == "read_resource") != (self.resource_path_sha256 is not None):
            raise ValueError("Skill资源访问事件路径摘要不一致")
        if (self.outcome == "succeeded") != (self.result_sha256 is not None):
            raise ValueError("Skill访问结果摘要不一致")
        if (self.outcome == "failed") != (self.error_code is not None):
            raise ValueError("Skill访问错误码不一致")
        if self.digest != skill_access_event_digest(self):
            raise ValueError("Skill访问事件摘要不一致")
        return self


def skill_access_event_digest(event: SkillAccessEvent) -> str:
    return canonical_digest(event.model_dump(mode="json", exclude={"digest"}, warnings="error"))


def build_skill_access_event(
    *,
    catalog_id: str,
    sequence: int,
    generation: int,
    catalog_sha256: str,
    operation: SkillAccessOperation,
    manifest_sha256: str,
    resource_path_sha256: str | None,
    outcome: SkillAccessOutcome,
    result_sha256: str | None,
    error_code: str | None,
    occurred_at: datetime,
    previous_digest: str | None,
) -> SkillAccessEvent:
    candidate = SkillAccessEvent.model_construct(
        _fields_set=None,
        catalog_id=catalog_id,
        sequence=sequence,
        generation=generation,
        catalog_sha256=catalog_sha256,
        operation=operation,
        manifest_sha256=manifest_sha256,
        resource_path_sha256=resource_path_sha256,
        outcome=outcome,
        result_sha256=result_sha256,
        error_code=error_code,
        occurred_at=occurred_at,
        previous_digest=previous_digest,
        digest="0" * 64,
    )
    return SkillAccessEvent(
        **candidate.model_dump(exclude={"digest"}),
        digest=skill_access_event_digest(candidate),
    )
