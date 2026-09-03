from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from harnessix.domain.models import ContractModel

if TYPE_CHECKING:
    from harnessix.agent.usage import UsageObservation

BillingLabel = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:/-]+$")]
_LABELS = ("service_tier", "inference_geo")
_COUNTS = ("cache_creation_5m_tokens", "cache_creation_1h_tokens")


class ResponseBillingMetadata(ContractModel):
    """响应中观测到的原生事实，不推断平台/价格，不代表实际账单。"""

    service_tier: BillingLabel | None = None
    inference_geo: BillingLabel | None = None
    cache_creation_5m_tokens: int | None = Field(default=None, ge=0, strict=True)
    cache_creation_1h_tokens: int | None = Field(default=None, ge=0, strict=True)

    @property
    def observed(self) -> bool:
        return any(getattr(self, field) is not None for field in (*_LABELS, *_COUNTS))

    def validate_successor(self, previous: ResponseBillingMetadata) -> None:
        for field in _LABELS:
            before, after = getattr(previous, field), getattr(self, field)
            if before is not None and before != after:
                raise ValueError("已知响应计费属性不可改变或丢失")
        for field in _COUNTS:
            before, after = getattr(previous, field), getattr(self, field)
            if before is not None and (after is None or after < before):
                raise ValueError("缓存 TTL 累计分项不可回退或丢失")

    def validate_usage(self, usage: UsageObservation) -> None:
        subtotal = (self.cache_creation_5m_tokens or 0) + (self.cache_creation_1h_tokens or 0)
        if (
            usage.cache_creation_input_tokens is not None
            and subtotal > usage.cache_creation_input_tokens
        ):
            raise ValueError("缓存 TTL 分项超过已知写入总量")
