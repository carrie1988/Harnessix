"""产品就绪合同：定义配置草案、写入收据与启动预检事实。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from harnessix.execution.contracts import canonical_digest
from harnessix.models.config import ModelHTTPConfig
from harnessix.product_config.contracts import (
    ConfigurationDiagnosticReport,
    ProductConfigContract,
    ProviderKind,
)
from harnessix.tools.contracts import Revision

PreflightMode = Literal["startup", "doctor"]
PreflightPlatform = Literal["posix", "windows"]
PreflightCategory = Literal[
    "config",
    "profile",
    "dependency",
    "secret",
    "workspace",
    "state",
    "platform",
    "tui",
    "git",
]
PreflightRequirement = Literal["required", "advisory"]
PreflightStatus = Literal["passed", "failed", "skipped"]

_IDENTIFIER = r"^[a-z][a-z0-9_-]{0,63}$"
_SECRET_NAME = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"


def _single_line(value: str) -> str:
    if not value.strip() or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("配置文本必须是非空单行")
    return value


class ConfigurationDraft(ProductConfigContract):
    """配置向导的非敏感最小输入；API Key值不属于该合同。"""

    spec_version: Literal["harnessix.configuration-draft/v1"] = "harnessix.configuration-draft/v1"
    provider_kind: ProviderKind
    provider_id: str = Field(default="primary", pattern=_IDENTIFIER)
    profile_id: str = Field(default="primary", pattern=_IDENTIFIER)
    base_url: str = Field(max_length=2048)
    model: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
    secret_name: str = Field(default="model-api-key", pattern=_SECRET_NAME)
    secret_version: str = Field(default="environment-v1", min_length=1, max_length=128)
    environment_variable: str = Field(default="MODEL_API_KEY", pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
    output_token_parameter: Literal["max_completion_tokens", "max_tokens"] | None = None

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        return ModelHTTPConfig.validate_url(value)

    @field_validator("secret_version")
    @classmethod
    def valid_secret_version(cls, value: str) -> str:
        return _single_line(value)

    @model_validator(mode="after")
    def valid_provider_options(self) -> Self:
        if self.provider_kind == "anthropic" and self.output_token_parameter is not None:
            raise ValueError("Anthropic配置草案不能包含OpenAI输出Token参数")
        return self


class ConfigurationWriteReceipt(ProductConfigContract):
    """配置文件原子创建或CAS替换的脱敏收据。"""

    spec_version: Literal["harnessix.configuration-write-receipt/v1"] = (
        "harnessix.configuration-write-receipt/v1"
    )
    operation: Literal["created", "replaced"]
    previous_source_sha256: Revision | None = None
    source_sha256: Revision
    config_sha256: Revision
    occurred_at: AwareDatetime
    receipt_sha256: Revision

    @model_validator(mode="after")
    def valid_receipt(self) -> Self:
        if (self.operation == "created") != (
            self.previous_source_sha256 is None
        ) or self.receipt_sha256 != configuration_write_receipt_digest(self):
            raise ValueError("配置写入收据不一致")
        return self


def configuration_write_receipt_digest(receipt: ConfigurationWriteReceipt) -> str:
    return canonical_digest(
        receipt.model_dump(mode="json", exclude={"receipt_sha256"}, warnings="error")
    )


class ProductPreflightCheck(ProductConfigContract):
    """一项不含原始异常或宿主路径的产品启动检查。"""

    check_id: str = Field(pattern=r"^product_[a-z0-9_]{1,119}$")
    category: PreflightCategory
    requirement: PreflightRequirement
    status: PreflightStatus
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,127}$")
    remediation_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{1,127}$",
    )
    duration_ms: int = Field(ge=0, le=300_000)


class ProductPreflightReport(ProductConfigContract):
    """Preflight与Doctor共享的版本化、摘要绑定事实。"""

    spec_version: Literal["harnessix.product-preflight/v1"] = "harnessix.product-preflight/v1"
    mode: PreflightMode
    platform: PreflightPlatform
    workspace_fingerprint: Revision
    config_sha256: Revision | None = None
    selected_profile: str | None = Field(default=None, pattern=_IDENTIFIER)
    configuration: ConfigurationDiagnosticReport | None = None
    checks: tuple[ProductPreflightCheck, ...] = Field(min_length=1, max_length=64)
    ready: bool
    generated_at: AwareDatetime
    report_sha256: Revision

    @model_validator(mode="after")
    def valid_report(self) -> Self:
        identifiers = [item.check_id for item in self.checks]
        configuration = self.configuration
        if configuration is None:
            configuration_bound = self.config_sha256 is None and self.selected_profile is None
        else:
            configuration_bound = (
                self.config_sha256 == configuration.config_sha256
                and self.selected_profile == configuration.selected_profile
            )
        expected_ready = (
            configuration is not None
            and configuration.ready
            and all(
                item.status == "passed" for item in self.checks if item.requirement == "required"
            )
        )
        if (
            identifiers != sorted(identifiers)
            or len(set(identifiers)) != len(identifiers)
            or not configuration_bound
            or self.ready != expected_ready
            or self.report_sha256 != product_preflight_report_digest(self)
        ):
            raise ValueError("产品Preflight报告不一致")
        return self


def product_preflight_report_digest(report: ProductPreflightReport) -> str:
    return canonical_digest(
        report.model_dump(mode="json", exclude={"report_sha256"}, warnings="error")
    )
