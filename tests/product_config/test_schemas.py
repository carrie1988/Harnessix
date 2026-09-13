from __future__ import annotations

import json
from pathlib import Path

from harnessix.product_config.codec import decode_product_config_bytes
from harnessix.product_config.contracts import (
    ConfigAuditEvent,
    ConfigMigrationReceipt,
    ConfigurationDiagnosticReport,
    ProductConfigSnapshot,
    ProductConfigV1,
    ProductConfigV2,
    ProfileSelection,
    ProviderFallbackDecision,
)
from harnessix.product_config.product_contracts import (
    ConfigurationDraft,
    ConfigurationWriteReceipt,
    ProductPreflightReport,
)


def test_committed_product_config_schemas_match_runtime_contracts() -> None:
    root = Path(__file__).parents[2] / "spec"
    expected = {
        "product-config-v1.schema.json": ProductConfigV1.model_json_schema(),
        "product-config-v2.schema.json": ProductConfigV2.model_json_schema(),
        "product-config-snapshot-v1.schema.json": ProductConfigSnapshot.model_json_schema(),
        "profile-selection-v1.schema.json": ProfileSelection.model_json_schema(),
        "configuration-draft-v1.schema.json": ConfigurationDraft.model_json_schema(),
        "configuration-diagnostic-v1.schema.json": (
            ConfigurationDiagnosticReport.model_json_schema()
        ),
        "configuration-write-receipt-v1.schema.json": (
            ConfigurationWriteReceipt.model_json_schema()
        ),
        "product-preflight-v1.schema.json": ProductPreflightReport.model_json_schema(),
        "config-migration-receipt-v1.schema.json": ConfigMigrationReceipt.model_json_schema(),
        "config-audit-event-v1.schema.json": ConfigAuditEvent.model_json_schema(),
        "provider-fallback-decision-v1.schema.json": (ProviderFallbackDecision.model_json_schema()),
    }
    for name, schema in expected.items():
        assert json.loads((root / name).read_text(encoding="utf-8")) == schema


def test_committed_v2_example_is_a_valid_strict_product_config() -> None:
    root = Path(__file__).parents[2]
    document = decode_product_config_bytes(
        (root / "docs/examples/product-config-v2.json").read_bytes()
    )
    assert isinstance(document, ProductConfigV2)
    assert [profile.profile_id for profile in document.profile_chain()] == [
        "primary",
        "backup",
    ]
