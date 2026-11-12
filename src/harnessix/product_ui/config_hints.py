"""启动时对现有配置的提示：只读配置、只给建议，不改文件。"""

from __future__ import annotations

from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigSnapshot, effective_thinking
from harnessix.product_config.wizard import effort_tiers_verified

UNBOUNDED_THINKING_HINT = (
    "提示：当前配置没有设置思考强度，这个模型会按最强档思考，单步可能要几分钟。"
    "重新运行 harnessix code configure 可改为默认的中档（已有会话不受影响）。"
)


def thinking_config_hint(config: Path, profile: str | None) -> str | None:
    """活动模型核实过思考强度档位、配置却既没设强度也没设上限时给出提示；读不到配置时不提示。

    0.10.0b22及更早生成的配置都是这样：模型按服务端默认的最强档思考（真实试用里单步十分钟）。
    """
    try:
        snapshot = load_product_config(config)
        if not isinstance(snapshot, ProductConfigSnapshot):
            return None
        active = snapshot.config.profile_chain(profile)[0]
    except (OSError, ValueError, KeyError, IndexError, KernelError):
        return None
    provider = next(
        item for item in snapshot.config.providers if item.provider_id == active.provider_id
    )
    unbounded = active.thinking_effort is None and active.thinking_budget_tokens is None
    thinks = (
        effective_thinking(active, provider) != "off"
        if provider.thinking_protocol is not None
        # 没有思考协议的旧配置不发送任何思考参数：这些型号在服务端默认思考。
        else True
    )
    if unbounded and thinks and effort_tiers_verified(provider.base_url, active.model):
        return UNBOUNDED_THINKING_HINT
    return None
