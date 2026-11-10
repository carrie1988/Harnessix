"""按官方牌价估算本轮费用上限；只覆盖已核对的模型，结果不是账单。

牌价核对于2026-11-09。公开协议没有缓存命中量，输入全部按未命中价计算，实际费用通常更低。
DeepSeek按高峰价计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit

from harnessix.agent.errors import KernelError
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigSnapshot

_MILLION: Final = Decimal(1_000_000)
_SYMBOLS: Final = {"CNY": "¥", "USD": "$"}
_REMINDERS: Final = {
    "CNY": tuple(Decimal(item) for item in (5, 20, 50, 100, 200)),
    "USD": tuple(Decimal(item) for item in (1, 5, 10, 20, 50)),
}


@dataclass(frozen=True, slots=True)
class ListPrice:
    """每百万Token的牌价。"""

    currency: Literal["CNY", "USD"]
    input_per_million: Decimal
    output_per_million: Decimal


@dataclass(frozen=True, slots=True)
class CostUpperBound:
    currency: Literal["CNY", "USD"]
    amount: Decimal

    def text(self) -> str:
        """费用上限文案；越过提醒档位后提示用户可以取消。"""

        symbol = _SYMBOLS[self.currency]
        # 上限按牌价且不扣缓存折扣估算，实际账单通常更低；说明见用户手册，状态栏只放数字。
        base = f"费用≤{symbol}{self.amount:.4f}"
        crossed = [item for item in _REMINDERS[self.currency] if self.amount >= item]
        return f"{base} · 已超过 {symbol}{crossed[-1]}，可按 Ctrl+X 取消" if crossed else base


_LIST_PRICES: Final[tuple[tuple[str, str, ListPrice], ...]] = (
    ("api.deepseek.com", "deepseek-v4-pro", ListPrice("USD", Decimal("1.32"), Decimal("3.96"))),
    ("api.deepseek.com", "deepseek-flash", ListPrice("USD", Decimal("0.3"), Decimal("1.2"))),
    ("dashscope.aliyuncs.com", "deepseek-v4-pro", ListPrice("CNY", Decimal("12"), Decimal("24"))),
    ("dashscope.aliyuncs.com", "qwen3.8-max", ListPrice("CNY", Decimal("12"), Decimal("36"))),
    ("dashscope.aliyuncs.com", "qwen3.8-flash", ListPrice("CNY", Decimal("0.8"), Decimal("2.7"))),
)


def list_price_for(base_url: str, model: str) -> ListPrice | None:
    host = urlsplit(base_url).hostname
    for known_host, prefix, price in _LIST_PRICES:
        if host == known_host and model.startswith(prefix):
            return price
    return None


def chain_list_price(config: Path, profile: str | None) -> ListPrice | None:
    """只有活动Profile没有回退链时才能确定价格；读不到配置时按未知处理。"""

    try:
        snapshot = load_product_config(config)
        if not isinstance(snapshot, ProductConfigSnapshot):
            return None
        chain = snapshot.config.profile_chain(profile)
    except (OSError, ValueError, KeyError, KernelError):
        return None
    if len(chain) != 1:
        return None
    provider = next(
        item for item in snapshot.config.providers if item.provider_id == chain[0].provider_id
    )
    return list_price_for(provider.base_url, chain[0].model)


def cost_upper_bound(price: ListPrice, *, input_tokens: int, output_tokens: int) -> CostUpperBound:
    amount = input_tokens * price.input_per_million + output_tokens * price.output_per_million
    return CostUpperBound(price.currency, amount / _MILLION)


def tokens_for_cost(price: ListPrice, amount: Decimal) -> int:
    """把金额上限按输入单价折算成累计Token；输入占绝大多数，输出的差额可以忽略。"""

    return max(1, int(amount * _MILLION / price.input_per_million))
