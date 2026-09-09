from __future__ import annotations

import json
from pathlib import Path

import pytest

from harnessix.product_config.contracts import (
    EnvironmentSecretSourceConfig,
    ModelCapabilities,
    ModelProfile,
    ProductConfigV2,
    ProviderDefinition,
    SecretReference,
)


def product_config() -> ProductConfigV2:
    primary_secret = SecretReference(name="primary-api-key", version="v1")
    backup_secret = SecretReference(name="backup-api-key", version="v2")
    return ProductConfigV2(
        active_profile="primary",
        secret_sources=(
            EnvironmentSecretSourceConfig(
                secret=backup_secret, environment_variable="BACKUP_API_KEY"
            ),
            EnvironmentSecretSourceConfig(
                secret=primary_secret, environment_variable="PRIMARY_API_KEY"
            ),
        ),
        providers=(
            ProviderDefinition(
                provider_id="backup",
                kind="anthropic",
                base_url="https://api.anthropic.test",
                credential=backup_secret,
            ),
            ProviderDefinition(
                provider_id="primary",
                kind="openai_chat",
                base_url="https://api.openai.test/v1",
                credential=primary_secret,
            ),
        ),
        profiles=(
            ModelProfile(
                profile_id="backup",
                provider_id="backup",
                model="claude-test",
                capabilities=ModelCapabilities(),
            ),
            ModelProfile(
                profile_id="primary",
                provider_id="primary",
                model="gpt-test",
                capabilities=ModelCapabilities(),
                fallback_profiles=("backup",),
            ),
        ),
    )


@pytest.fixture
def config() -> ProductConfigV2:
    return product_config()


def write_config(path: Path, config: ProductConfigV2) -> Path:
    path.write_text(
        json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path
