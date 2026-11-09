from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from harnessix.exec_cli import _cost_summary
from harnessix.product_ui.cost_estimate import ListPrice, cost_upper_bound, list_price_for


@pytest.mark.parametrize(
    ("base_url", "model", "known"),
    [
        ("https://api.deepseek.com/v1", "deepseek-v4-pro", True),
        ("https://api.deepseek.com/v1", "deepseek-flash", True),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-flash", True),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-27b", False),
        ("https://api.example.test/v1", "deepseek-v4-pro", False),
    ],
)
def test_prices_only_cover_verified_models(base_url: str, model: str, known: bool) -> None:
    assert (list_price_for(base_url, model) is not None) is known


def test_upper_bound_charges_all_input_at_the_full_rate() -> None:
    bound = cost_upper_bound(
        ListPrice("CNY", Decimal("12"), Decimal("24")),
        input_tokens=1_000_000,
        output_tokens=500_000,
    )

    assert bound.amount == Decimal("24")
    assert bound.text() == "费用不超过 ¥24.0000（牌价估算，未扣缓存折扣）"


def _turn(input_tokens: int, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    )


def test_exec_summary_reports_an_upper_bound_only_for_priced_models() -> None:
    price = ListPrice("CNY", Decimal("12"), Decimal("24"))

    priced = _cost_summary(_turn(1_000_000, 0), "unknown", price)
    unpriced = _cost_summary(_turn(1_000_000, 0), "unknown", None)
    local = _cost_summary(_turn(1_000_000, 0), "free", price)

    assert priced == {
        "pricing": "unknown",
        "amount_cny": None,
        "upper_bound": {"currency": "CNY", "amount": "12"},
    }
    assert unpriced == {"pricing": "unknown", "amount_cny": None}
    assert local == {"pricing": "free", "amount_cny": 0}
