from __future__ import annotations

from harnessix.agent.billing import ResponseBillingMetadata


def merge_billing(
    previous: ResponseBillingMetadata,
    *,
    service_tier: object = None,
    inference_geo: object = None,
    cache_creation: object = None,
) -> ResponseBillingMetadata:
    values = previous.model_dump()
    updates = {"service_tier": service_tier, "inference_geo": inference_geo}
    if cache_creation is not None:
        if not isinstance(cache_creation, dict):
            raise ValueError("缓存 TTL 明细必须为对象")
        updates.update(
            cache_creation_5m_tokens=cache_creation.get("ephemeral_5m_input_tokens"),
            cache_creation_1h_tokens=cache_creation.get("ephemeral_1h_input_tokens"),
        )
    values.update({field: value for field, value in updates.items() if value is not None})
    result = ResponseBillingMetadata.model_validate(values)
    result.validate_successor(previous)
    return result
