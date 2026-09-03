import pytest

from harnessix.agent.billing import ResponseBillingMetadata
from harnessix.agent.usage import UsageObservation
from harnessix.models.billing import resolve_billing_context
from harnessix.models.costs import CostReport, bind_price, build_cost_report, estimate_attempt
from harnessix.models.pricing import BillingContext
from tests.models.pricing_helpers import attempt, context, price, turn


def source(**changes):
    return attempt(
        provider="anthropic",
        billing=ResponseBillingMetadata(service_tier="standard", inference_geo="us", **changes),
    )


def host(**changes):
    return BillingContext(billing_provider="anthropic", inference_mode="verified-mode", **changes)


def test_direct_provider_facts_fill_context_but_do_not_infer_mode_or_platform():
    recorded = source()
    resolved = resolve_billing_context(recorded, host())
    assert resolved.region == "us" and resolved.service_tier == "standard"
    assert resolved.inference_mode == "verified-mode"
    empty = BillingContext()
    assert resolve_billing_context(recorded, empty) == empty
    assert (
        resolve_billing_context(
            recorded, BillingContext(billing_provider="anthropic")
        ).inference_mode
        is None
    )


@pytest.mark.parametrize("verified", [host(service_tier="priority"), host(region="global")])
def test_conflicting_host_context_rejected(verified):
    with pytest.raises(ValueError, match="冲突"):
        resolve_billing_context(source(), verified)


@pytest.mark.parametrize("platform", [None, "proxy", "aliyun_bailian", "openai"])
def test_never_attribute_native_anthropic_labels_to_another_platform(platform):
    verified = BillingContext(billing_provider=platform, service_tier="custom")
    assert resolve_billing_context(source(), verified) == verified


@pytest.mark.parametrize(
    "short,long,ttl",
    [(3, 0, "5m"), (0, 3, "1h"), (2, 1, None), (1, 0, None), (None, 3, None), (None, None, None)],
)
def test_only_complete_single_ttl_partitions_resolve(short, long, ttl):
    recorded = source(cache_creation_5m_tokens=short, cache_creation_1h_tokens=long)
    assert resolve_billing_context(recorded, host()).cache_write_ttl == ttl


@pytest.mark.parametrize("short,long,ttl", [(2, 1, "5m"), (1, 0, "5m"), (0, 3, "5m"), (3, 0, "1h")])
def test_mixed_incomplete_and_conflicting_ttl_cannot_be_forced(short, long, ttl):
    recorded = source(cache_creation_5m_tokens=short, cache_creation_1h_tokens=long)
    with pytest.raises(ValueError):
        resolve_billing_context(recorded, host(cache_write_ttl=ttl))


def test_no_observation_allows_explicit_verified_host_and_does_not_fill_missing():
    recorded = attempt(provider="anthropic")
    assert resolve_billing_context(recorded, host()) == host()
    explicit = host(region="us", service_tier="standard", cache_write_ttl="5m")
    assert resolve_billing_context(recorded, explicit) == explicit


@pytest.mark.parametrize("ttl", [None, "5m", "1h"])
def test_zero_cache_write_does_not_infer_or_conflict_with_irrelevant_ttl(ttl):
    recorded = attempt(
        provider="anthropic",
        usage=UsageObservation(
            completeness="complete",
            input_tokens=7,
            output_tokens=2,
            uncached_input_tokens=3,
            cache_read_input_tokens=4,
            cache_creation_input_tokens=0,
        ),
        billing=ResponseBillingMetadata(cache_creation_5m_tokens=0, cache_creation_1h_tokens=0),
    )
    assert resolve_billing_context(recorded, host(cache_write_ttl=ttl)).cache_write_ttl == ttl


def test_price_binding_uses_observation_and_report_v1_stays_recomputable():
    recorded = source(cache_creation_5m_tokens=3, cache_creation_1h_tokens=0)
    snapshot = price(
        billing_provider="anthropic",
        region="us",
        service_tier="standard",
        inference_mode="verified-mode",
    )
    binding = bind_price(recorded, snapshot, host())
    assert binding.context.cache_write_ttl == "5m"
    report = build_cost_report(turn(recorded), (binding,))
    assert report.summary.completeness == "complete"
    assert CostReport.model_validate_json(report.model_dump_json()) == report
    assert "billing" not in report.entries[0].attempt.model_dump()


def test_legacy_binding_cannot_bypass_new_live_facts():
    before = attempt()
    binding = bind_price(
        before, price(), context(billing_provider="openai", service_tier="default")
    )
    after = before.model_copy(update={"billing": ResponseBillingMetadata(service_tier="priority")})
    with pytest.raises(ValueError, match="冲突"):
        estimate_attempt(after, binding)
    with pytest.raises(ValueError, match="冲突"):
        build_cost_report(turn(after), (binding,))


def test_missing_context_in_prebuilt_binding_cannot_skip_resolution():
    before = attempt()
    binding = bind_price(before, price(), context(billing_provider="openai", service_tier=None))
    after = before.model_copy(update={"billing": ResponseBillingMetadata(service_tier="priority")})
    with pytest.raises(ValueError, match="未纳入"):
        estimate_attempt(after, binding)


def test_mixed_ttl_cost_unknown_not_single_rate():
    recorded = source(cache_creation_5m_tokens=2, cache_creation_1h_tokens=1)
    snapshot = price(
        billing_provider="anthropic",
        region="us",
        service_tier="standard",
        inference_mode="verified-mode",
    )
    result = estimate_attempt(recorded, bind_price(recorded, snapshot, host())).result
    assert (
        result.status == "unknown"
        and result.reason == "cache_ttl_missing"
        and result.amount is None
    )
