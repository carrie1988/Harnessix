"""产品配置：汇总并导出受支持的公共入口，不承载运行时编排。"""

from harnessix.product_config.contracts import (
    ConfigAuditEvent,
    ConfigMigrationReceipt,
    ConfigurationDiagnostic,
    ConfigurationDiagnosticReport,
    EnvironmentSecretSourceConfig,
    ModelCapabilities,
    ModelProfile,
    ProductConfigSnapshot,
    ProductConfigV1,
    ProductConfigV2,
    ProfileSelection,
    ProviderDefinition,
    ProviderFallbackDecision,
    SecretReference,
)

__all__ = [
    "ConfigAuditEvent",
    "ConfigMigrationReceipt",
    "ConfigurationDiagnostic",
    "ConfigurationDiagnosticReport",
    "EnvironmentSecretSourceConfig",
    "ModelCapabilities",
    "ModelProfile",
    "ProductConfigSnapshot",
    "ProductConfigV1",
    "ProductConfigV2",
    "ProfileSelection",
    "ProviderDefinition",
    "ProviderFallbackDecision",
    "SecretReference",
]
