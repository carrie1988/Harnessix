"""产品配置：汇总并导出受支持的公共入口，不承载运行时编排。"""

from harnessix.product_config.contracts import (
    ConfigAuditEvent,
    ConfigMigrationReceipt,
    ConfigurationDiagnostic,
    ConfigurationDiagnosticReport,
    ConfigurationDraft,
    ConfigurationWriteReceipt,
    EnvironmentSecretSourceConfig,
    ModelCapabilities,
    ModelProfile,
    ProductConfigSnapshot,
    ProductConfigV1,
    ProductConfigV2,
    ProductPreflightCheck,
    ProductPreflightReport,
    ProfileSelection,
    ProviderDefinition,
    ProviderFallbackDecision,
    SecretReference,
)
from harnessix.product_config.preflight import ProductPreflightRequest, run_product_preflight
from harnessix.product_config.wizard import (
    ConfigurationWriteRequest,
    build_product_config,
    write_product_config,
)

__all__ = [
    "ConfigAuditEvent",
    "ConfigMigrationReceipt",
    "ConfigurationDraft",
    "ConfigurationDiagnostic",
    "ConfigurationDiagnosticReport",
    "ConfigurationWriteReceipt",
    "ConfigurationWriteRequest",
    "EnvironmentSecretSourceConfig",
    "ModelCapabilities",
    "ModelProfile",
    "ProductConfigSnapshot",
    "ProductConfigV1",
    "ProductConfigV2",
    "ProductPreflightCheck",
    "ProductPreflightReport",
    "ProductPreflightRequest",
    "ProfileSelection",
    "ProviderDefinition",
    "ProviderFallbackDecision",
    "SecretReference",
    "build_product_config",
    "run_product_preflight",
    "write_product_config",
]
