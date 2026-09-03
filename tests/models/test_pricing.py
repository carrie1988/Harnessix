from datetime import timedelta
from decimal import getcontext, localcontext

import pytest
from pydantic import ValidationError

from harnessix.agent.usage import UsageObservation
from harnessix.models.costs import bind_price, estimate_attempt
from harnessix.models.pricing import PriceSnapshot
from tests.models.pricing_helpers import NOW, attempt, context, price


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        0,
        1.0,
        -1,
        "-1",
        "NaN",
        "Infinity",
        "1e2",
        "01",
        ".1",
        " 1",
        "1\n",
        "0.0000000000001",
        "1000000000000",
    ],
)
def test_rates_reject_coercion_nonfinite_and_unsupported_precision(value):
    with pytest.raises(ValidationError):
        price(output_per_million=value)


@pytest.mark.parametrize(
    "value",
    [
        "http://prices.invalid",
        "https://user:secret@prices.invalid",
        "https://prices.invalid/?token=secret",
        "https://prices.invalid/#secret",
        "https://",
    ],
)
def test_price_source_has_no_credentials_or_query(value):
    with pytest.raises(ValueError):
        price(source_url=value)


@pytest.mark.parametrize(
    "change",
    [
        {"currency": "EUR"},
        {"unexpected": 1},
        {"valid_until": NOW, "valid_from": NOW},
        {"input_tokens_min": 10, "input_tokens_max": 9},
        {"input_tokens_min": True},
        {"valid_from": "2026-09-03T00:00:00"},
        {"spec_version": "harnessix.price/v2"},
    ],
)
def test_price_contract_is_strict(change):
    with pytest.raises(ValueError):
        price(**change)


def test_price_roundtrip_and_digest_are_content_bound():
    original = price()
    assert PriceSnapshot.model_validate_json(original.model_dump_json()) == original
    assert price(output_per_million="5").version == original.version
    assert price(output_per_million="5").digest != original.digest


def test_price_source_preserves_trailing_slash():
    assert price(source_url="https://prices.invalid/fixture/").source_url.endswith("/fixture/")


def test_maximum_rate_retains_all_fractional_digits():
    source = attempt(
        usage=UsageObservation(completeness="complete", input_tokens=1, output_tokens=0)
    )
    fixed = price(
        input_price={"kind": "flat", "per_million": "999999999999.999999999999"},
        output_per_million="0",
    )
    result = estimate_attempt(source, bind_price(source, fixed, context())).result
    assert result.amount == "999999.999999999999999999"


def test_ttl_independent_price_must_be_explicit():
    data = price().model_dump()
    data["input_price"]["cache_write_ttl"] = None
    fixed = PriceSnapshot.model_validate(data)
    source = attempt()
    assert (
        estimate_attempt(
            source, bind_price(source, fixed, context(cache_write_ttl=None))
        ).result.amount
        == "0.000025"
    )
    del data["input_price"]["cache_write_ttl"]
    with pytest.raises(ValueError):
        PriceSnapshot.model_validate(data)


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
def test_complete_usage_is_priced_even_when_attempt_failed(status):
    value = attempt(status=status)
    result = estimate_attempt(value, bind_price(value, price(), context()))
    assert result.result.amount == "0.000025"
    assert sum(line.tokens for line in result.result.lines) == 12
    assert [line.category for line in result.result.lines] == [
        "uncached_input",
        "cache_read_input",
        "cache_creation_input",
        "output",
    ]
    encoded = result.model_dump_json()
    assert "raw-error-canary" not in encoded and "response-private-canary" not in encoded
    assert "requested-alias" not in encoded


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"status": "running"}, "attempt_running"),
        ({"status": "failed", "usage": UsageObservation()}, "usage_incomplete"),
        (
            {
                "status": "failed",
                "usage": UsageObservation(completeness="partial", input_tokens=10),
            },
            "usage_incomplete",
        ),
        ({"status": "failed", "actual_model": None}, "model_unknown"),
        ({"actual_model": "other-model"}, "scope_mismatch"),
        (
            {"usage": UsageObservation(completeness="complete", input_tokens=10, output_tokens=2)},
            "usage_details_missing",
        ),
    ],
)
def test_unknown_attempts_never_become_zero(changes, expected):
    value = attempt(**changes)
    result = estimate_attempt(value, bind_price(value, price(), context())).result
    assert result.status == "unknown" and result.reason == expected
    assert result.amount is None and not result.lines


@pytest.mark.parametrize("field", ["billing_provider", "region", "service_tier", "inference_mode"])
@pytest.mark.parametrize("value", [None, "different"])
def test_context_must_be_known_and_match(field, value):
    source = attempt()
    result = estimate_attempt(source, bind_price(source, price(), context(**{field: value}))).result
    assert result.reason == ("context_missing" if value is None else "scope_mismatch")
    assert result.amount is None


@pytest.mark.parametrize(
    ("ttl", "reason"), [(None, "cache_ttl_missing"), ("1h", "cache_ttl_mismatch"), ("5m", None)]
)
def test_required_ttl_is_not_defaulted(ttl, reason):
    source = attempt()
    result = estimate_attempt(
        source, bind_price(source, price(), context(cache_write_ttl=ttl))
    ).result
    assert result.reason == reason
    assert (result.amount is None) == (reason is not None)


def test_ttl_is_irrelevant_when_no_cache_writes():
    source = attempt(
        usage=UsageObservation(
            completeness="complete",
            input_tokens=10,
            output_tokens=2,
            uncached_input_tokens=6,
            cache_read_input_tokens=4,
            cache_creation_input_tokens=0,
        )
    )
    result = estimate_attempt(
        source, bind_price(source, price(), context(cache_write_ttl=None))
    ).result
    assert result.amount == "0.000022"


@pytest.mark.parametrize(
    ("changes", "known"),
    [
        ({"valid_from": NOW}, True),
        ({"valid_from": NOW + timedelta(microseconds=1)}, False),
        ({"valid_until": NOW + timedelta(seconds=1)}, False),
        ({"valid_until": NOW + timedelta(seconds=1, microseconds=1)}, True),
        ({"input_tokens_min": 10, "input_tokens_max": 10}, True),
        ({"input_tokens_min": 11}, False),
        ({"input_tokens_max": 9}, False),
    ],
)
def test_price_window_and_whole_request_input_band(changes, known):
    source = attempt()
    result = estimate_attempt(source, bind_price(source, price(**changes), context())).result
    assert (result.status == "estimated") == known
    if not known:
        assert result.reason in {"outside_price_period", "outside_input_band"}


@pytest.mark.parametrize("tokens", [1, 10**50])
def test_fixed_point_cost_is_exact_independent_of_decimal_context(tokens):
    source = attempt(
        usage=UsageObservation(completeness="complete", input_tokens=tokens, output_tokens=0)
    )
    fixed = price(
        input_price={"kind": "flat", "per_million": "0.000000000001"}, output_per_million="0"
    )
    with localcontext():
        getcontext().prec = 2
        result = estimate_attempt(source, bind_price(source, fixed, context())).result
    assert result.amount == ("0.000000000000000001" if tokens == 1 else str(10**32))


def test_unknown_price_and_explicit_zero_are_distinct():
    source = attempt()
    assert estimate_attempt(source).result.amount is None
    fixed = price(input_price={"kind": "flat", "per_million": "0"}, output_per_million="0")
    result = estimate_attempt(source, bind_price(source, fixed, context())).result
    assert result.amount == "0" and result.status == "estimated"


def test_binding_cannot_be_reused_after_attempt_changes():
    source = attempt()
    binding = bind_price(source, price(), context())
    for other in [attempt(), source.model_copy(update={"finished_at": NOW + timedelta(seconds=2)})]:
        with pytest.raises(ValueError, match="不属于当前尝试快照"):
            estimate_attempt(other, binding)
    with pytest.raises(ValueError):
        estimate_attempt(
            source, binding.model_copy(update={"price": price(output_per_million="5")})
        )
