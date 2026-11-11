"""按官方牌价估算本轮费用上限；只覆盖已核对的模型，结果不是账单。

牌价核对于2026-11-10（百炼模型价格页、DeepSeek中文价格页、智谱API定价页）。公开协议没有缓存命中量，输入全部按
未命中价计算，实际费用通常更低。DeepSeek按高峰价计算；国内账户以人民币计费（余额接口返回CNY）。
只按核对过的确切型号匹配：带日期的版本可能另有价格（如百炼deepseek-v4-pro-0813的输出单价高于
deepseek-v4-pro），未登记的型号显示费用未知，避免给出偏低的“上限”。
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
# 产品面向中国用户，金额一律以人民币显示；只登记以人民币计费的已核对牌价。
_SYMBOLS: Final = {"CNY": "¥"}
_REMINDERS: Final = {"CNY": tuple(Decimal(item) for item in (5, 20, 50, 100, 200))}


@dataclass(frozen=True, slots=True)
class ListPrice:
    """每百万Token的牌价。"""

    currency: Literal["CNY"]
    input_per_million: Decimal
    output_per_million: Decimal


@dataclass(frozen=True, slots=True)
class CostUpperBound:
    currency: Literal["CNY"]
    amount: Decimal

    def text(self) -> str:
        """费用上限文案；越过提醒档位后提示用户可以取消。"""

        symbol = _SYMBOLS[self.currency]
        # 上限按牌价且不扣缓存折扣估算，实际账单通常更低；说明见用户手册，状态栏只放数字。
        base = f"费用≤{symbol}{self.amount:.4f}"
        crossed = [item for item in _REMINDERS[self.currency] if self.amount >= item]
        return f"{base} · 已超过 {symbol}{crossed[-1]}，可按 Ctrl+X 取消" if crossed else base


_DASHSCOPE: Final = "dashscope.aliyuncs.com"
_DEEPSEEK: Final = "api.deepseek.com"
_ZHIPU: Final = "open.bigmodel.cn"
_LIST_PRICES: Final[dict[tuple[str, str], ListPrice]] = {
    (_DEEPSEEK, "deepseek-flash"): ListPrice("CNY", Decimal("2"), Decimal("8")),
    # 旧模型名仍可调用，由Flash模型提供服务并按Flash价格计费（DeepSeek价格页注1）。
    (_DEEPSEEK, "deepseek-v4-flash"): ListPrice("CNY", Decimal("2"), Decimal("8")),
    (_DEEPSEEK, "deepseek-v4-pro"): ListPrice("CNY", Decimal("9"), Decimal("27")),
    (_DASHSCOPE, "deepseek-v4-pro"): ListPrice("CNY", Decimal("12"), Decimal("24")),
    (_DASHSCOPE, "deepseek-v4-pro-0813"): ListPrice("CNY", Decimal("9"), Decimal("27")),
    (_DASHSCOPE, "qwen3.8-max"): ListPrice("CNY", Decimal("12"), Decimal("36")),
    (_DASHSCOPE, "qwen3.8-max-0902"): ListPrice("CNY", Decimal("12"), Decimal("36")),
    (_DASHSCOPE, "qwen3.8-flash"): ListPrice("CNY", Decimal("0.8"), Decimal("2.7")),
    (_ZHIPU, "glm-5.3"): ListPrice("CNY", Decimal("8"), Decimal("28")),
    (_ZHIPU, "glm-5.3-flash"): ListPrice("CNY", Decimal("0.8"), Decimal("2.8")),
    (_ZHIPU, "glm-5.3-flashx"): ListPrice("CNY", Decimal("2"), Decimal("7")),
}


def list_price_for(base_url: str, model: str) -> ListPrice | None:
    """只返回确切型号的已核对牌价。"""

    return _LIST_PRICES.get((urlsplit(base_url).hostname or "", model))


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


def chain_model_name(config: Path, profile: str | None) -> str | None:
    """活动Profile的首选模型名，供状态栏显示；读不到配置时返回None。"""

    try:
        snapshot = load_product_config(config)
        if not isinstance(snapshot, ProductConfigSnapshot):
            return None
        return snapshot.config.profile_chain(profile)[0].model
    except (OSError, ValueError, KeyError, IndexError, KernelError):
        return None


def cost_upper_bound(price: ListPrice, *, input_tokens: int, output_tokens: int) -> CostUpperBound:
    amount = input_tokens * price.input_per_million + output_tokens * price.output_per_million
    return CostUpperBound(price.currency, amount / _MILLION)


def tokens_for_cost(price: ListPrice, amount: Decimal) -> int:
    """把金额上限按输入单价折算成累计Token；输入占绝大多数，输出的差额可以忽略。"""

    return max(1, int(amount * _MILLION / price.input_per_million))
