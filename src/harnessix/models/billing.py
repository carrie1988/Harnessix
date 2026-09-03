from __future__ import annotations

from harnessix.agent.usage import ModelAttempt
from harnessix.models.pricing import BillingContext


def resolve_billing_context(attempt: ModelAttempt, verified: BillingContext) -> BillingContext:
    """仅对宿主明确声明的直连平台使用原生响应属性；不自动识别代理计费规则。"""
    attempt = ModelAttempt.model_validate_json(attempt.model_dump_json())
    verified = BillingContext.model_validate_json(verified.model_dump_json())
    direct_openai = attempt.provider == "openai_chat" and verified.billing_provider == "openai"
    direct_anthropic = attempt.provider == "anthropic" and verified.billing_provider == "anthropic"
    if not (direct_openai or direct_anthropic):
        return verified
    observed = attempt.billing
    updates: dict[str, object] = {}
    if observed.service_tier is not None and observed.service_tier != "auto":
        updates["service_tier"] = observed.service_tier
    if direct_anthropic:
        if observed.inference_geo is not None:
            updates["region"] = observed.inference_geo
        short, long = observed.cache_creation_5m_tokens, observed.cache_creation_1h_tokens
        total = attempt.usage.cache_creation_input_tokens
        if (short is not None or long is not None) and total != 0:
            ttl = None
            if short is not None and long is not None and total == short + long and total > 0:
                if short == 0:
                    ttl = "1h"
                elif long == 0:
                    ttl = "5m"
            if ttl is None and verified.cache_write_ttl is not None:
                raise ValueError("已观测分项不能证实宿主指定的单一缓存 TTL")
            updates["cache_write_ttl"] = ttl
    for field, value in updates.items():
        current = getattr(verified, field)
        if current is not None and current != value:
            raise ValueError("宿主计费上下文与响应事实冲突")
    return BillingContext.model_validate({**verified.model_dump(), **updates})
