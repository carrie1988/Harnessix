"""产品Action配置与能力报告：定义不含Secret值的版本化严格合同。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from harnessix.domain.models import utc_now
from harnessix.execution.contracts import NetworkMode, canonical_digest
from harnessix.product_config.contracts import ProductConfigContract, SecretReference
from harnessix.tools.contracts import Revision
from harnessix.workspace.contracts import PlatformKind

ActionCapabilityKind = Literal["artifact", "workspace_patch", "process_profile"]
ActionCapabilityStatus = Literal["verified", "omitted"]
SelectorPolicy = Literal["none", "bounded_test_selector"]
ProductActionConfigSource = Literal["builtin", "file"]
ProductActionConfigOperation = Literal["loaded", "activated"]

_IDENTIFIER = r"^[a-z][a-z0-9_-]{0,63}$"
_EXECUTOR_ID = r"^[a-z][a-z0-9_.-]{0,127}$"
_IMAGE_DIGEST = r"^[a-z0-9][a-z0-9._/-]{0,255}@sha256:[0-9a-f]{64}$"
_CONTAINER_PROGRAM = r"^/[A-Za-z0-9._/+:-]+(?:/[A-Za-z0-9._+:-]+)*$"


class ProductProcessProfile(ProductConfigContract):
    """宿主持有的固定容器进程Profile；模型只能选择，不能改写执行边界。"""

    spec_version: Literal["harnessix.product-process-profile/v1"] = (
        "harnessix.product-process-profile/v1"
    )
    profile_id: str = Field(pattern=_IDENTIFIER)
    version: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=500)
    container_engine: str = Field(min_length=1, max_length=4096)
    image: str = Field(pattern=_IMAGE_DIGEST)
    program: str = Field(pattern=_CONTAINER_PROGRAM)
    arguments: tuple[str, ...] = Field(default=(), max_length=128)
    selector_policy: SelectorPolicy = "none"
    timeout_seconds: int = Field(default=300, ge=1, le=3600, strict=True)
    max_output_bytes: int = Field(default=1_048_576, ge=1024, le=8_388_608, strict=True)
    network_mode: NetworkMode = "none"
    cpu_limit: float = Field(default=1.0, gt=0, le=32, allow_inf_nan=False, strict=True)
    memory_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=16 * 1024 * 1024,
        le=64 * 1024 * 1024 * 1024,
        strict=True,
    )
    process_limit: int = Field(default=128, ge=1, le=4096, strict=True)
    secret_refs: tuple[SecretReference, ...] = Field(default=(), max_length=16)
    profile_sha256: Revision

    @field_validator("version", "description", "container_engine")
    @classmethod
    def bounded_single_line(cls, value: str) -> str:
        if not value.strip() or "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError("Process Profile文本必须是非空单行")
        return value

    @field_validator("container_engine")
    @classmethod
    def absolute_engine(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("Container Engine必须是宿主绝对路径")
        return value

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            type(item) is not str
            or not item
            or len(item.encode("utf-8")) > 4096
            or "\x00" in item
            or "\r" in item
            or "\n" in item
            for item in value
        ):
            raise ValueError("Process Profile参数不符合边界")
        return value

    @field_validator("secret_refs")
    @classmethod
    def canonical_secrets(cls, value: tuple[SecretReference, ...]) -> tuple[SecretReference, ...]:
        identities = [(item.name, item.version) for item in value]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError("Process Profile Secret引用必须排序且唯一")
        return value

    @model_validator(mode="after")
    def valid_profile(self) -> Self:
        if self.network_mode != "none":
            raise ValueError("Product Action v1只允许无网络Process Profile")
        if self.profile_sha256 != product_process_profile_digest(self):
            raise ValueError("Process Profile摘要不一致")
        return self


def product_process_profile_digest(profile: ProductProcessProfile) -> str:
    return canonical_digest(
        profile.model_dump(mode="json", exclude={"profile_sha256"}, warnings="error")
    )


def build_product_process_profile(
    *,
    profile_id: str,
    version: str,
    description: str,
    container_engine: str,
    image: str,
    program: str,
    arguments: tuple[str, ...] = (),
    selector_policy: SelectorPolicy = "none",
    timeout_seconds: int = 300,
    max_output_bytes: int = 1_048_576,
    network_mode: NetworkMode = "none",
    cpu_limit: float = 1.0,
    memory_bytes: int = 512 * 1024 * 1024,
    process_limit: int = 128,
    secret_refs: tuple[SecretReference, ...] = (),
) -> ProductProcessProfile:
    candidate = ProductProcessProfile.model_construct(
        _fields_set=None,
        profile_id=profile_id,
        version=version,
        description=description,
        container_engine=container_engine,
        image=image,
        program=program,
        arguments=arguments,
        selector_policy=selector_policy,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        network_mode=network_mode,
        cpu_limit=cpu_limit,
        memory_bytes=memory_bytes,
        process_limit=process_limit,
        secret_refs=secret_refs,
        profile_sha256="0" * 64,
    )
    return ProductProcessProfile(
        **candidate.model_dump(exclude={"profile_sha256"}),
        profile_sha256=product_process_profile_digest(candidate),
    )


class ProductActionConfigV1(ProductConfigContract):
    """独立于Product Config v2的Action功能门与固定Process Profile。"""

    spec_version: Literal["harnessix.product-action-config/v1"] = (
        "harnessix.product-action-config/v1"
    )
    workspace_patch_enabled: bool = True
    process_profiles: tuple[ProductProcessProfile, ...] = Field(default=(), max_length=32)
    config_sha256: Revision

    @model_validator(mode="after")
    def canonical_config(self) -> Self:
        identities = [item.profile_id for item in self.process_profiles]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError("Process Profile必须按ID排序且唯一")
        if self.config_sha256 != product_action_config_digest(self):
            raise ValueError("Product Action配置摘要不一致")
        return self


def product_action_config_digest(config: ProductActionConfigV1) -> str:
    return canonical_digest(
        config.model_dump(mode="json", exclude={"config_sha256"}, warnings="error")
    )


def build_product_action_config(
    *,
    workspace_patch_enabled: bool = True,
    process_profiles: tuple[ProductProcessProfile, ...] = (),
) -> ProductActionConfigV1:
    candidate = ProductActionConfigV1.model_construct(
        _fields_set=None,
        workspace_patch_enabled=workspace_patch_enabled,
        process_profiles=process_profiles,
        config_sha256="0" * 64,
    )
    return ProductActionConfigV1(
        **candidate.model_dump(exclude={"config_sha256"}),
        config_sha256=product_action_config_digest(candidate),
    )


class ProductActionConfigSnapshot(ProductConfigContract):
    """绑定安全读取来源、原始字节摘要与规范Action配置的不可变快照。"""

    spec_version: Literal["harnessix.product-action-config-snapshot/v1"] = (
        "harnessix.product-action-config-snapshot/v1"
    )
    source_kind: ProductActionConfigSource
    source_sha256: Revision
    config_sha256: Revision
    loaded_at: AwareDatetime
    config: ProductActionConfigV1

    @model_validator(mode="after")
    def valid_snapshot(self) -> Self:
        if self.config_sha256 != product_action_config_digest(self.config):
            raise ValueError("Product Action配置快照摘要不一致")
        return self


class ProductActionConfigAuditEvent(ProductConfigContract):
    """Action配置加载与激活的连续Hash链事件。"""

    spec_version: Literal["harnessix.product-action-config-audit-event/v1"] = (
        "harnessix.product-action-config-audit-event/v1"
    )
    sequence: int = Field(ge=1)
    operation: ProductActionConfigOperation
    config_sha256: Revision
    previous_active_sha256: Revision | None = None
    previous_digest: Revision | None = None
    occurred_at: AwareDatetime
    digest: Revision

    @model_validator(mode="after")
    def valid_event(self) -> Self:
        if (
            (self.sequence == 1) != (self.previous_digest is None)
            or (self.operation == "loaded" and self.previous_active_sha256 is not None)
            or self.digest != product_action_config_audit_event_digest(self)
        ):
            raise ValueError("Product Action配置审计事件不一致")
        return self


def product_action_config_audit_event_digest(event: ProductActionConfigAuditEvent) -> str:
    return canonical_digest(event.model_dump(mode="json", exclude={"digest"}, warnings="error"))


class ProductActionStartupRecoveryReport(ProductConfigContract):
    """一次冷启动Action恢复的脱敏汇总；逐Plan事实保留在Action Audit中。"""

    spec_version: Literal["harnessix.product-action-startup-recovery/v1"] = (
        "harnessix.product-action-startup-recovery/v1"
    )
    candidate_config_sha256: Revision
    recovery_config_sha256: Revision
    scanned_routes: int = Field(ge=0)
    interrupted_routes: int = Field(ge=0)
    reconciled_routes: int = Field(ge=0)
    succeeded_routes: int = Field(ge=0)
    failed_routes: int = Field(ge=0)
    manual_intervention_routes: int = Field(ge=0)
    unresolved_routes: int = Field(ge=0)
    pending_approval_routes: int = Field(ge=0)
    ready_routes: int = Field(ge=0)
    created_at: AwareDatetime
    report_sha256: Revision

    @model_validator(mode="after")
    def valid_report(self) -> Self:
        terminal = self.succeeded_routes + self.failed_routes + self.manual_intervention_routes
        if (
            self.interrupted_routes > self.scanned_routes
            or self.reconciled_routes > self.scanned_routes
            or terminal + self.unresolved_routes != self.reconciled_routes
            or self.pending_approval_routes + self.ready_routes > self.scanned_routes
            or self.report_sha256 != product_action_startup_recovery_report_digest(self)
        ):
            raise ValueError("Product Action启动恢复报告不一致")
        return self


def product_action_startup_recovery_report_digest(
    report: ProductActionStartupRecoveryReport,
) -> str:
    return canonical_digest(
        report.model_dump(
            mode="json",
            exclude={"report_sha256"},
            warnings="error",
        )
    )


class ProductActionCapabilityEvidence(ProductConfigContract):
    """一次有时效的能力证明或诚实省略事实。"""

    spec_version: Literal["harnessix.product-action-capability/v1"] = (
        "harnessix.product-action-capability/v1"
    )
    capability_id: str = Field(pattern=_EXECUTOR_ID)
    kind: ActionCapabilityKind
    status: ActionCapabilityStatus
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    platform: PlatformKind
    binding_digest: Revision | None = None
    executor_evidence_digest: Revision | None = None
    probed_at: AwareDatetime
    expires_at: AwareDatetime
    evidence_sha256: Revision

    @model_validator(mode="after")
    def complete_evidence(self) -> Self:
        verified = self.status == "verified"
        if (
            verified != (self.binding_digest is not None)
            or verified != (self.executor_evidence_digest is not None)
            or self.expires_at <= self.probed_at
            or self.expires_at - self.probed_at > timedelta(minutes=10)
            or self.evidence_sha256 != product_action_capability_digest(self)
        ):
            raise ValueError("Product Action能力证据不完整")
        return self


def product_action_capability_digest(evidence: ProductActionCapabilityEvidence) -> str:
    return canonical_digest(
        evidence.model_dump(mode="json", exclude={"evidence_sha256"}, warnings="error")
    )


def build_product_action_capability(
    *,
    capability_id: str,
    kind: ActionCapabilityKind,
    status: ActionCapabilityStatus,
    reason_code: str,
    platform: PlatformKind,
    binding_digest: str | None = None,
    executor_evidence_digest: str | None = None,
    probed_at: datetime | None = None,
    ttl_seconds: int = 300,
) -> ProductActionCapabilityEvidence:
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 600:
        raise ValueError("Product Action能力证据TTL必须为1至600秒")
    now = probed_at or utc_now()
    candidate = ProductActionCapabilityEvidence.model_construct(
        _fields_set=None,
        capability_id=capability_id,
        kind=kind,
        status=status,
        reason_code=reason_code,
        platform=platform,
        binding_digest=binding_digest,
        executor_evidence_digest=executor_evidence_digest,
        probed_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        evidence_sha256="0" * 64,
    )
    return ProductActionCapabilityEvidence(
        **candidate.model_dump(exclude={"evidence_sha256"}),
        evidence_sha256=product_action_capability_digest(candidate),
    )


class ProductActionCapabilityReport(ProductConfigContract):
    """产品启动时唯一的Action能力广告与省略事实。"""

    spec_version: Literal["harnessix.product-action-capability-report/v1"] = (
        "harnessix.product-action-capability-report/v1"
    )
    config_sha256: Revision
    capabilities: tuple[ProductActionCapabilityEvidence, ...] = Field(max_length=64)
    created_at: AwareDatetime
    report_sha256: Revision

    @model_validator(mode="after")
    def canonical_report(self) -> Self:
        identities = [item.capability_id for item in self.capabilities]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError("Product Action能力必须排序且唯一")
        if any(
            self.created_at < item.probed_at or self.created_at >= item.expires_at
            for item in self.capabilities
        ):
            raise ValueError("Product Action能力报告时间不在证据有效期内")
        if self.report_sha256 != product_action_capability_report_digest(self):
            raise ValueError("Product Action能力报告摘要不一致")
        return self


def product_action_capability_report_digest(report: ProductActionCapabilityReport) -> str:
    return canonical_digest(
        report.model_dump(mode="json", exclude={"report_sha256"}, warnings="error")
    )


def build_product_action_capability_report(
    config: ProductActionConfigV1,
    capabilities: tuple[ProductActionCapabilityEvidence, ...],
    *,
    created_at: datetime | None = None,
) -> ProductActionCapabilityReport:
    candidate = ProductActionCapabilityReport.model_construct(
        _fields_set=None,
        config_sha256=config.config_sha256,
        capabilities=capabilities,
        created_at=created_at or utc_now(),
        report_sha256="0" * 64,
    )
    return ProductActionCapabilityReport(
        **candidate.model_dump(exclude={"report_sha256"}),
        report_sha256=product_action_capability_report_digest(candidate),
    )
