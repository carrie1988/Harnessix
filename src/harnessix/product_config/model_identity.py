"""告诉模型它在这次会话里是谁：配置的模型名与服务商，来自用户配置（Beta Issue #17）。

0.10.0b32：百炼qwen3.8-flash两轮自称“Anthropic的Claude”，并否认自己是千问；状态栏与会话记录都是qwen3.8-flash。
运行时指令此前完全没有身份说明，模型被问到时按训练数据作答。说明按配置生成，任何模型与服务商都适用。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from urllib.parse import urlsplit

from harnessix.context.contracts import ContextFragment, ContextFragmentKind
from harnessix.product_config.contracts import ModelProfile, ProviderDefinition
from harnessix.product_config.vendors import VENDORS, endpoint_host

MODEL_IDENTITY_SOURCE: Final = "harnessix.model-identity/v1"
_VENDOR_NAMES: Final = {urlsplit(vendor.base_url).hostname: vendor.name for vendor in VENDORS}


def model_identity_fragment(
    chain: Sequence[ModelProfile], providers: Sequence[ProviderDefinition]
) -> ContextFragment:
    """按Profile链的顺序写出模型名与服务商；第一项是主模型，其余是主模型不可用时的回退。"""

    endpoints = {provider.provider_id: provider.base_url for provider in providers}
    served = [_served_by(profile.model, endpoints.get(profile.provider_id)) for profile in chain]
    fallback = (
        f" If it is unavailable, requests fall back to {', then '.join(served[1:])}."
        if len(served) > 1
        else ""
    )
    content = (
        "Model identity, from the user's configuration: requests in this session go to "
        f"{served[0]}."
        f"{fallback} You are running inside Harnessix Code, a coding agent. When asked which model "
        "or company you are, answer from this configuration; never claim to be a model or vendor "
        "that it does not name."
    )
    return ContextFragment(
        kind=ContextFragmentKind.RUNTIME_INSTRUCTION, source=MODEL_IDENTITY_SOURCE, content=content
    )


def _served_by(model: str, base_url: str | None) -> str:
    host = None if base_url is None else endpoint_host(base_url)
    if host is None:
        return f'the model "{model}"'
    vendor = _VENDOR_NAMES.get(host)
    where = host if vendor is None else f"{vendor} ({host})"
    return f'the model "{model}" served by {where}'
