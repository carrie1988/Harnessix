"""在验收镜像中生成本地Provider原生配置；不接触真实服务凭据。"""

from pathlib import Path

from harnessix.product_config.contracts import (
    EnvironmentSecretSourceConfig,
    ModelProfile,
    ProductConfigV2,
    ProviderDefinition,
    SecretReference,
)

secret = SecretReference(name="fixture-api-key", version="v1")
config = ProductConfigV2(
    active_profile="fixture",
    secret_sources=(
        EnvironmentSecretSourceConfig(
            secret=secret, environment_variable="HARNESSIX_FIXTURE_API_KEY"
        ),
    ),
    providers=(
        ProviderDefinition(
            provider_id="fixture",
            kind="openai_chat",
            base_url="https://127.0.0.1:8411/v1",
            credential=secret,
        ),
    ),
    profiles=(
        ModelProfile(
            profile_id="fixture",
            provider_id="fixture",
            model="fixture-coding",
            max_output_tokens=512,
        ),
    ),
)
path = Path("/opt/harnessix/product.json")
path.write_text(config.model_dump_json())
path.chmod(0o600)
