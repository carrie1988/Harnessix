from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from harnessix.agent.usage import ModelIdentifier, TokenCount
from harnessix.domain.models import ContractModel
from harnessix.models.config import ModelHTTPConfig

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:/-]+$")]
Rate = Annotated[
    str, Field(strict=True, max_length=25, pattern=r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,12})?$")
]
Amount = Annotated[str, Field(strict=True, pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{1,18})?$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Currency = Literal["USD", "CNY"]
CacheTTL = Literal["5m", "1h"]


def content_digest(value: ContractModel) -> str:
    encoded = json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def rate_units(rate: str) -> int:
    """每百万单价的 12 位定点整数；乘 Token 后即为 10^-18 货币单位。"""
    whole, _, fraction = rate.partition(".")
    return int(whole) * 10**12 + int(fraction.ljust(12, "0"))


def amount_units(amount: str) -> int:
    whole, _, fraction = amount.partition(".")
    return int(whole) * 10**18 + int(fraction.ljust(18, "0"))


def format_amount(units: int) -> str:
    whole, fraction = divmod(units, 10**18)
    return f"{whole}.{fraction:018d}".rstrip("0").rstrip(".") if fraction else str(whole)


class FlatInputPrice(ContractModel):
    kind: Literal["flat"] = "flat"
    per_million: Rate


class PartitionedInputPrice(ContractModel):
    kind: Literal["partitioned"] = "partitioned"
    uncached_per_million: Rate
    cache_read_per_million: Rate
    cache_creation_per_million: Rate
    # 必填的 null 表示本快照费率不依赖 TTL，不是默认采用 5 分钟。
    cache_write_ttl: CacheTTL | None


class BillingContext(ContractModel):
    """可信宿主核对的计费上下文；不是从 Adapter 类型或请求模型猜测。"""

    billing_provider: Identifier | None = None
    region: Identifier | None = None
    service_tier: Identifier | None = None
    inference_mode: Identifier | None = None
    cache_write_ttl: CacheTTL | None = None


class PriceSnapshot(ContractModel):
    spec_version: Literal["harnessix.price/v1"] = "harnessix.price/v1"
    version: Identifier
    source_url: str = Field(max_length=2048)
    billing_provider: Identifier
    model: ModelIdentifier
    region: Identifier
    service_tier: Identifier
    inference_mode: Identifier
    currency: Currency
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    input_tokens_min: TokenCount
    input_tokens_max: TokenCount | None
    input_price: Annotated[FlatInputPrice | PartitionedInputPrice, Field(discriminator="kind")]
    output_per_million: Rate

    @field_validator("source_url")
    @classmethod
    def validate_source(cls, value: str) -> str:
        ModelHTTPConfig.validate_url(value)
        return value

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.valid_until <= self.valid_from:
            raise ValueError("价格生效区间必须非空")
        if self.input_tokens_max is not None and self.input_tokens_max < self.input_tokens_min:
            raise ValueError("输入价格区间无效")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self)
