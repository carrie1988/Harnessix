import json


def billing_frames(adapter, *, tier=None, geo="us", short=3, long=0):
    parts = adapter.detailed_frames()
    if adapter.kind == "openai":
        value = json.loads(parts[0].decode().split("data: ", 1)[1])
        value["service_tier"] = tier or "default"
        parts[0] = adapter.wire.frame(value)
    else:
        parts[0] = adapter.wire.start(
            input_tokens=3,
            cache_read_input_tokens=4,
            cache_creation_input_tokens=3,
            service_tier=tier or "standard",
            inference_geo=geo,
            cache_creation={"ephemeral_5m_input_tokens": short, "ephemeral_1h_input_tokens": long},
        )
    return parts
