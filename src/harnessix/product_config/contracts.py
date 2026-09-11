"""产品配置：定义版本化数据合同及其跨字段一致性校验。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from harnessix.domain.models import ContractModel
from harnessix.execution.contracts import canonical_digest
from harnessix.models.config import ModelHTTPConfig
from harnessix.tools.contracts import Revision

ProviderKind = Literal["openai_chat", "anthropic"]
ConfigAuditOperation = Literal["loaded", "activated", "migrated"]
DiagnosticStatus = Literal["passed", "failed"]
DiagnosticScope = Literal["config", "profile", "provider", "secret", "dependency"]

_IDENTIFIER = r"^[a-z][a-z0-9_-]{0,63}$"
_SECRET_NAME = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"


class ProductConfigContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


def _single_line(value: str) -> str:
    if not value.strip() or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("配置文本必须是非空单行")
    return value


class SecretReference(ProductConfigContract):
    name: str = Field(pattern=_SECRET_NAME)
    version: str = Field(min_length=1, max_length=128)

    @field_validator("version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        return _single_line(value)


class EnvironmentSecretSourceConfig(ProductConfigContract):
    secret: SecretReference
    environment_variable: str = Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")


class ProviderDefinition(ProductConfigContract):
    provider_id: str = Field(pattern=_IDENTIFIER)
    kind: ProviderKind
    base_url: str = Field(max_length=2048)
    credential: SecretReference
    output_token_parameter: Literal["max_completion_tokens", "max_tokens"] | None = None

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        return ModelHTTPConfig.validate_url(value)

    @model_validator(mode="after")
    def provider_options(self) -> Self:
        if self.kind == "anthropic" and self.output_token_parameter is not None:
            raise ValueError("Anthropic不能配置OpenAI输出Token参数")
        return self


class ModelCapabilities(ProductConfigContract):
    tool_calls: bool = True
    parallel_tool_calls: bool = True
    streaming_usage: Literal[True] = True

    @model_validator(mode="after")
    def valid_tools(self) -> Self:
        if self.parallel_tool_calls and not self.tool_calls:
            raise ValueError("并行工具能力要求工具调用能力")
        return self

    def satisfies(self, required: ModelCapabilities) -> bool:
        return (
            (not required.tool_calls or self.tool_calls)
            and (not required.parallel_tool_calls or self.parallel_tool_calls)
            and self.streaming_usage
        )


class ModelProfile(ProductConfigContract):
    profile_id: str = Field(pattern=_IDENTIFIER)
    provider_id: str = Field(pattern=_IDENTIFIER)
    model: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    required_capabilities: ModelCapabilities = Field(
        default_factory=lambda: ModelCapabilities(
            tool_calls=False,
            parallel_tool_calls=False,
        )
    )
    fallback_profiles: tuple[str, ...] = Field(default=(), max_length=5)
    max_output_tokens: int = Field(default=4096, ge=1, le=1_000_000)
    timeout_seconds: float = Field(default=120, gt=0, le=3600)
    io_timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_attempts: int = Field(default=2, ge=1, le=5)
    retry_delay_seconds: float = Field(default=0.5, ge=0, le=10)
    max_request_bytes: int = Field(default=2_097_152, ge=1024, le=16_777_216)
    max_response_bytes: int = Field(default=2_097_152, ge=1024, le=16_777_216)
    max_frame_bytes: int = Field(default=262_144, ge=128, le=1_048_576)
    max_chunks: int = Field(default=10_000, ge=1, le=100_000)

    @field_validator("fallback_profiles")
    @classmethod
    def canonical_fallbacks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not item or len(item) > 64 for item in value):
            raise ValueError("Fallback Profile必须唯一且有效")
        return value

    @model_validator(mode="after")
    def valid_profile(self) -> Self:
        if self.profile_id in self.fallback_profiles:
            raise ValueError("Profile不能Fallback到自身")
        if not self.capabilities.satisfies(self.required_capabilities):
            raise ValueError("Profile声明能力不能满足自身要求")
        return self


class ProductConfigV2(ProductConfigContract):
    spec_version: Literal["harnessix.product-config/v2"] = "harnessix.product-config/v2"
    active_profile: str = Field(pattern=_IDENTIFIER)
    secret_sources: tuple[EnvironmentSecretSourceConfig, ...] = Field(min_length=1, max_length=32)
    providers: tuple[ProviderDefinition, ...] = Field(min_length=1, max_length=16)
    profiles: tuple[ModelProfile, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def valid_graph(self) -> Self:
        source_names = [item.secret.name for item in self.secret_sources]
        environment_variables = [item.environment_variable for item in self.secret_sources]
        provider_ids = [item.provider_id for item in self.providers]
        profile_ids = [item.profile_id for item in self.profiles]
        if source_names != sorted(source_names) or len(set(source_names)) != len(source_names):
            raise ValueError("Secret Source必须按名称排序且唯一")
        if len(set(environment_variables)) != len(environment_variables):
            raise ValueError("环境变量只能定位一个Secret引用")
        if provider_ids != sorted(provider_ids) or len(set(provider_ids)) != len(provider_ids):
            raise ValueError("Provider必须按ID排序且唯一")
        if profile_ids != sorted(profile_ids) or len(set(profile_ids)) != len(profile_ids):
            raise ValueError("Profile必须按ID排序且唯一")
        if self.active_profile not in profile_ids:
            raise ValueError("活动Profile不存在")
        sources = {item.secret.name: item.secret for item in self.secret_sources}
        providers = {item.provider_id: item for item in self.providers}
        profiles = {item.profile_id: item for item in self.profiles}
        for provider in self.providers:
            if sources.get(provider.credential.name) != provider.credential:
                raise ValueError("Provider Secret引用不存在或版本不一致")
        for profile in self.profiles:
            if profile.provider_id not in providers:
                raise ValueError("Profile Provider引用不存在")
            if any(item not in profiles for item in profile.fallback_profiles):
                raise ValueError("Fallback Profile引用不存在")
            chain = self.profile_chain(profile.profile_id)
            if len(chain) > 6 or sum(item.max_attempts for item in chain) > 32:
                raise ValueError("Fallback链超过Profile或尝试账本上限")
            if any(
                not candidate.capabilities.satisfies(profile.required_capabilities)
                for candidate in chain
            ):
                raise ValueError("Fallback候选不能满足首选Profile要求能力")
        return self

    def profile_chain(self, profile_id: str | None = None) -> tuple[ModelProfile, ...]:
        profiles = {item.profile_id: item for item in self.profiles}
        selected = profile_id or self.active_profile
        if selected not in profiles:
            raise ValueError("Profile不存在")
        ordered: list[ModelProfile] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(identity: str) -> None:
            if identity in visiting:
                raise ValueError("Fallback Profile存在环")
            if identity in visited:
                raise ValueError("Fallback Profile展开后重复")
            visiting.add(identity)
            profile = profiles[identity]
            ordered.append(profile)
            for fallback in profile.fallback_profiles:
                visit(fallback)
            visiting.remove(identity)
            visited.add(identity)

        visit(selected)
        return tuple(ordered)


class LegacyProviderDefinition(ProductConfigContract):
    provider_id: str = Field(pattern=_IDENTIFIER)
    kind: ProviderKind
    base_url: str = Field(max_length=2048)
    api_key_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
    output_token_parameter: Literal["max_completion_tokens", "max_tokens"] | None = None

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        return ModelHTTPConfig.validate_url(value)

    @model_validator(mode="after")
    def provider_options(self) -> Self:
        if self.kind == "anthropic" and self.output_token_parameter is not None:
            raise ValueError("Anthropic不能配置OpenAI输出Token参数")
        return self


class ProductConfigV1(ProductConfigContract):
    spec_version: Literal["harnessix.product-config/v1"] = "harnessix.product-config/v1"
    active_profile: str = Field(pattern=_IDENTIFIER)
    providers: tuple[LegacyProviderDefinition, ...] = Field(min_length=1, max_length=16)
    profiles: tuple[ModelProfile, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def valid_references(self) -> Self:
        provider_ids = [item.provider_id for item in self.providers]
        profile_ids = [item.profile_id for item in self.profiles]
        if (
            provider_ids != sorted(provider_ids)
            or len(set(provider_ids)) != len(provider_ids)
            or profile_ids != sorted(profile_ids)
            or len(set(profile_ids)) != len(profile_ids)
            or self.active_profile not in profile_ids
            or any(item.provider_id not in provider_ids for item in self.profiles)
        ):
            raise ValueError("v1 Provider/Profile引用或顺序无效")
        # 复用v2图检查，Secret在迁移后补齐。
        for profile in self.profiles:
            profiles = {item.profile_id: item for item in self.profiles}
            if any(item not in profiles for item in profile.fallback_profiles):
                raise ValueError("v1 Fallback Profile引用不存在")
            _legacy_chain(profile.profile_id, profiles)
        return self


def _legacy_chain(profile_id: str, profiles: dict[str, ModelProfile]) -> tuple[ModelProfile, ...]:
    ordered: list[ModelProfile] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identity: str) -> None:
        if identity in visiting or identity in visited:
            raise ValueError("v1 Fallback Profile存在环或重复")
        visiting.add(identity)
        profile = profiles[identity]
        ordered.append(profile)
        for fallback in profile.fallback_profiles:
            visit(fallback)
        visiting.remove(identity)
        visited.add(identity)

    visit(profile_id)
    if len(ordered) > 6 or sum(item.max_attempts for item in ordered) > 32:
        raise ValueError("v1 Fallback链超过上限")
    return tuple(ordered)


def product_config_digest(config: ProductConfigV2) -> str:
    return canonical_digest(config.model_dump(mode="json", warnings="error"))


class ProductConfigSnapshot(ProductConfigContract):
    spec_version: Literal["harnessix.product-config-snapshot/v1"] = (
        "harnessix.product-config-snapshot/v1"
    )
    source_sha256: Revision
    config_sha256: Revision
    loaded_at: AwareDatetime
    config: ProductConfigV2

    @model_validator(mode="after")
    def valid_digest(self) -> Self:
        if self.config_sha256 != product_config_digest(self.config):
            raise ValueError("产品配置摘要不一致")
        return self


class ProfileSelection(ProductConfigContract):
    spec_version: Literal["harnessix.profile-selection/v1"] = "harnessix.profile-selection/v1"
    config_sha256: Revision
    selected_profile: str = Field(pattern=_IDENTIFIER)
    profile_chain: tuple[str, ...] = Field(min_length=1, max_length=6)
    provider_chain: tuple[str, ...] = Field(min_length=1, max_length=6)
    model_chain: tuple[str, ...] = Field(min_length=1, max_length=6)
    selection_sha256: Revision

    @model_validator(mode="after")
    def valid_selection(self) -> Self:
        if (
            self.selected_profile != self.profile_chain[0]
            or not (len(self.profile_chain) == len(self.provider_chain) == len(self.model_chain))
            or len(set(self.profile_chain)) != len(self.profile_chain)
            or self.selection_sha256 != profile_selection_digest(self)
        ):
            raise ValueError("Profile选择快照不规范")
        return self


def profile_selection_digest(selection: ProfileSelection) -> str:
    return canonical_digest(
        selection.model_dump(mode="json", exclude={"selection_sha256"}, warnings="error")
    )


class ConfigurationDiagnostic(ProductConfigContract):
    scope: DiagnosticScope
    subject_id: str = Field(min_length=1, max_length=128)
    code: str = Field(pattern=r"^config_[a-z0-9_]{1,119}$")
    status: DiagnosticStatus


class ConfigurationDiagnosticReport(ProductConfigContract):
    spec_version: Literal["harnessix.configuration-diagnostic/v1"] = (
        "harnessix.configuration-diagnostic/v1"
    )
    config_sha256: Revision
    selection_sha256: Revision
    selected_profile: str = Field(pattern=_IDENTIFIER)
    ready: bool
    checks: tuple[ConfigurationDiagnostic, ...] = Field(min_length=1, max_length=256)
    generated_at: AwareDatetime
    report_sha256: Revision

    @model_validator(mode="after")
    def valid_report(self) -> Self:
        keys = [(item.scope, item.subject_id, item.code) for item in self.checks]
        if (
            keys != sorted(keys)
            or len(set(keys)) != len(keys)
            or self.ready != all(item.status == "passed" for item in self.checks)
            or self.report_sha256 != diagnostic_report_digest(self)
        ):
            raise ValueError("配置诊断报告不规范")
        return self


def diagnostic_report_digest(report: ConfigurationDiagnosticReport) -> str:
    return canonical_digest(
        report.model_dump(mode="json", exclude={"report_sha256"}, warnings="error")
    )


class ConfigMigrationReceipt(ProductConfigContract):
    spec_version: Literal["harnessix.config-migration-receipt/v1"] = (
        "harnessix.config-migration-receipt/v1"
    )
    from_version: Literal["v1", "v2"]
    to_version: Literal["v2"] = "v2"
    source_sha256: Revision
    target_sha256: Revision
    backup_sha256: Revision | None = None
    changed: bool
    occurred_at: AwareDatetime
    receipt_sha256: Revision

    @model_validator(mode="after")
    def valid_receipt(self) -> Self:
        if (
            self.changed != (self.from_version == "v1")
            or self.changed != (self.backup_sha256 is not None)
            or (
                self.from_version == "v1"
                and (
                    self.backup_sha256 != self.source_sha256
                    or self.target_sha256 == self.source_sha256
                )
            )
            or (self.from_version == "v2" and self.target_sha256 != self.source_sha256)
            or self.receipt_sha256 != migration_receipt_digest(self)
        ):
            raise ValueError("配置迁移收据不一致")
        return self


def migration_receipt_digest(receipt: ConfigMigrationReceipt) -> str:
    return canonical_digest(
        receipt.model_dump(mode="json", exclude={"receipt_sha256"}, warnings="error")
    )


class ConfigAuditEvent(ProductConfigContract):
    spec_version: Literal["harnessix.config-audit-event/v1"] = "harnessix.config-audit-event/v1"
    sequence: int = Field(ge=1)
    operation: ConfigAuditOperation
    config_sha256: Revision
    profile_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    previous_active_sha256: Revision | None = None
    previous_active_profile: str | None = Field(default=None, pattern=_IDENTIFIER)
    migration_receipt_sha256: Revision | None = None
    previous_digest: Revision | None = None
    occurred_at: AwareDatetime
    digest: Revision

    @model_validator(mode="after")
    def valid_event(self) -> Self:
        previous_complete = (self.previous_active_sha256 is None) == (
            self.previous_active_profile is None
        )
        if (
            (self.sequence == 1) != (self.previous_digest is None)
            or (self.operation == "activated") != (self.profile_id is not None)
            or (self.operation == "migrated") != (self.migration_receipt_sha256 is not None)
            or not previous_complete
            or (self.operation != "activated" and self.previous_active_sha256 is not None)
            or self.digest != config_audit_event_digest(self)
        ):
            raise ValueError("配置审计事件不一致")
        return self


def config_audit_event_digest(event: ConfigAuditEvent) -> str:
    return canonical_digest(event.model_dump(mode="json", exclude={"digest"}, warnings="error"))


class ProviderFallbackDecision(ProductConfigContract):
    spec_version: Literal["harnessix.provider-fallback-decision/v1"] = (
        "harnessix.provider-fallback-decision/v1"
    )
    sequence: int = Field(ge=1)
    config_sha256: Revision
    thread_id: UUID
    turn_id: UUID
    step: int = Field(ge=1)
    from_profile: str = Field(pattern=_IDENTIFIER)
    from_provider: str = Field(pattern=_IDENTIFIER)
    to_profile: str = Field(pattern=_IDENTIFIER)
    to_provider: str = Field(pattern=_IDENTIFIER)
    failure_code: Literal["transport", "rate_limit", "provider_internal"]
    response_exposed: Literal[False] = False
    tool_call_exposed: Literal[False] = False
    previous_digest: Revision | None = None
    occurred_at: AwareDatetime
    digest: Revision

    @model_validator(mode="after")
    def valid_decision(self) -> Self:
        if (
            self.from_profile == self.to_profile
            or (self.sequence == 1) != (self.previous_digest is None)
            or self.digest != provider_fallback_decision_digest(self)
        ):
            raise ValueError("Provider Fallback决策不一致")
        return self


def provider_fallback_decision_digest(decision: ProviderFallbackDecision) -> str:
    return canonical_digest(decision.model_dump(mode="json", exclude={"digest"}, warnings="error"))


def build_profile_selection(
    config: ProductConfigV2, config_sha256: str, profile_id: str | None = None
) -> ProfileSelection:
    chain = config.profile_chain(profile_id)
    candidate = ProfileSelection.model_construct(
        _fields_set=None,
        config_sha256=config_sha256,
        selected_profile=chain[0].profile_id,
        profile_chain=tuple(item.profile_id for item in chain),
        provider_chain=tuple(item.provider_id for item in chain),
        model_chain=tuple(item.model for item in chain),
        selection_sha256="0" * 64,
    )
    return ProfileSelection(
        **candidate.model_dump(exclude={"selection_sha256"}),
        selection_sha256=profile_selection_digest(candidate),
    )


def build_migration_receipt(
    *,
    from_version: Literal["v1", "v2"],
    source_sha256: str,
    target_sha256: str,
    backup_sha256: str | None,
    occurred_at: datetime,
) -> ConfigMigrationReceipt:
    candidate = ConfigMigrationReceipt.model_construct(
        _fields_set=None,
        from_version=from_version,
        source_sha256=source_sha256,
        target_sha256=target_sha256,
        backup_sha256=backup_sha256,
        changed=from_version == "v1",
        occurred_at=occurred_at,
        receipt_sha256="0" * 64,
    )
    return ConfigMigrationReceipt(
        **candidate.model_dump(exclude={"receipt_sha256"}),
        receipt_sha256=migration_receipt_digest(candidate),
    )
