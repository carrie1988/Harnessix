from __future__ import annotations

import argparse
from decimal import Decimal
from types import SimpleNamespace

import pytest

from harnessix.exec_cli import _cost_summary, _positive_decimal
from harnessix.product_ui.cost_estimate import (
    CostUpperBound,
    ListPrice,
    cost_upper_bound,
    list_price_for,
    tokens_for_cost,
)
from harnessix.product_ui.turn_budget import (
    UNCAPPED_TOKENS,
    interactive_turn_budget,
    turn_budget_for,
)


@pytest.mark.parametrize(
    ("base_url", "model", "known"),
    [
        ("https://api.deepseek.com/v1", "deepseek-v4-pro", True),
        ("https://api.deepseek.com/v1", "deepseek-flash", True),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-flash", True),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-27b", False),
        ("https://api.example.test/v1", "deepseek-v4-pro", False),
        # 带日期或后缀的型号可能另有价格，不能套用前缀相同型号的牌价。
        ("https://api.deepseek.com/v1", "deepseek-v4-pro-0813", False),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-flash-0801", False),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "deepseek-v4-pro-0813", True),
    ],
)
def test_prices_only_cover_verified_models(base_url: str, model: str, known: bool) -> None:
    assert (list_price_for(base_url, model) is not None) is known


def test_deepseek_official_prices_are_peak_cny_and_dated_variants_differ() -> None:
    # 2026-11-10核对：国内账户按人民币计费，取高峰价作为上限。
    official = "https://api.deepseek.com"
    assert list_price_for(official, "deepseek-flash") == ListPrice(
        "CNY", Decimal("2"), Decimal("8")
    )
    assert list_price_for(official, "deepseek-v4-pro") == ListPrice(
        "CNY", Decimal("9"), Decimal("27")
    )
    bailian = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    pro = list_price_for(bailian, "deepseek-v4-pro")
    dated = list_price_for(bailian, "deepseek-v4-pro-0813")
    assert pro is not None and dated is not None
    assert dated.output_per_million > pro.output_per_million


def test_upper_bound_charges_all_input_at_the_full_rate() -> None:
    bound = cost_upper_bound(
        ListPrice("CNY", Decimal("12"), Decimal("24")),
        input_tokens=1_000_000,
        output_tokens=500_000,
    )

    assert bound.amount == Decimal("24")
    assert bound.text().startswith("费用≤¥24.0000 · ")


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


@pytest.mark.parametrize(
    ("amount", "suffix"),
    [("4", ""), ("5", "已超过 ¥5"), ("70", "已超过 ¥50")],
)
def test_upper_bound_reminds_at_each_threshold(amount: str, suffix: str) -> None:
    text = CostUpperBound("CNY", Decimal(amount)).text()

    assert ("已超过" in text) is bool(suffix)
    if suffix:
        assert suffix in text and "Ctrl+X" in text


def test_max_cost_converts_at_the_input_rate() -> None:
    price = ListPrice("CNY", Decimal("12"), Decimal("24"))

    assert tokens_for_cost(price, Decimal("6")) == 500_000


def test_interactive_budget_is_uncapped_only_when_cost_is_visible_or_free() -> None:
    assert interactive_turn_budget("free", cost_visible=False).max_tokens == UNCAPPED_TOKENS
    assert interactive_turn_budget("unknown", cost_visible=True).max_tokens == UNCAPPED_TOKENS
    capped = interactive_turn_budget("unknown", cost_visible=False)
    assert capped.max_tokens == turn_budget_for("unknown").max_tokens
    assert capped.max_steps == interactive_turn_budget("free", cost_visible=False).max_steps


@pytest.mark.parametrize("value", ["0", "-1", "abc", "NaN", "Infinity"])
def test_max_cost_argument_must_be_a_positive_number(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_decimal(value)
