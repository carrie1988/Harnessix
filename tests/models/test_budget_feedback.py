"""累计预算反馈覆盖实际映射，不把提示词当成预算强制机制。"""

import pytest

from harnessix.agent.models import Budget, Usage
from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._chat_mapping import build_request as chat_request
from harnessix.models._history import budget_instruction, encode_json
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from tests.contracts.provider import model_request
from tests.models.test_provider_input_budget import anchor_fixture, pure_provider


@pytest.mark.parametrize("adapter", ["chat", "anthropic"])
def test_current_budget_is_mapped_without_mutating_history_or_instructions(adapter):
    request = model_request().model_copy(
        update={
            "step": 4,
            "budget": Budget(max_tokens=50_000, max_steps=16),
            "consumed_usage": Usage(input_tokens=20_000, output_tokens=1500),
            "instructions": "runtime rules",
        }
    )
    before = request.model_dump_json()
    if adapter == "chat":
        body, _ = chat_request(request, OpenAIChatConfig(model="test"))
        assert body["messages"][0] == {"role": "system", "content": "runtime rules"}
        text = body["messages"][1]["content"]
        assert body["messages"][2]["content"] == request.history[0].content.text
    else:
        body, _ = anthropic_request(request, AnthropicConfig(model="test"))
        text = body["system"]
        assert text.startswith("runtime rules\n\n")
    assert "28500 cumulative input+output tokens and 13 model calls" in text
    assert "including this request and repeated history" in text
    assert request.model_dump_json() == before


def test_unspecified_feedback_preserves_legacy_wire_and_zero_is_not_missing():
    request = model_request()
    assert budget_instruction(request) is None
    body, _ = chat_request(request, OpenAIChatConfig(model="test"))
    assert len(body["messages"]) == 1
    body, _ = anthropic_request(request, AnthropicConfig(model="test"))
    assert "system" not in body
    first = request.model_copy(update={"consumed_usage": Usage()})
    assert f"{request.budget.max_tokens} cumulative" in budget_instruction(first)


def test_wire_estimator_charges_feedback_even_with_previous_wrapper_proof():
    provider, previous, current = anchor_fixture()
    current = current.model_copy(update={"consumed_usage": Usage(input_tokens=12_345)})
    with_note = provider.estimate_input_tokens(current, prefix_items=len(previous.history))
    without_note = provider.estimate_input_tokens(
        current.model_copy(update={"consumed_usage": None}), prefix_items=len(previous.history)
    )
    note = {"role": "system", "content": budget_instruction(current)}
    assert with_note - without_note >= len(encode_json(note).encode())
    body, _ = chat_request(current, provider.config)
    assert pure_provider().estimate_input_tokens(current) == len(encode_json(body).encode()) + 512


@pytest.mark.parametrize("adapter", ["chat", "anthropic"])
def test_input_bound_feedback_is_explicit_and_never_claims_usage(adapter):
    request = model_request().model_copy(
        update={"consumed_usage": Usage(input_tokens=18000), "input_tokens_upper_bound": 8500}
    )
    before = request.model_dump_json()
    mapper, config = (
        (chat_request, OpenAIChatConfig(model="test"))
        if adapter == "chat"
        else (anthropic_request, AnthropicConfig(model="test"))
    )
    body, _ = mapper(request, config)
    text = body["messages"][0]["content"] if adapter == "chat" else body["system"]
    assert "conservative input bound is 8500 tokens, not observed usage" in text
    assert "never patch without source evidence" in text
    assert "before the call limit" in text
    assert request.model_dump_json() == before


@pytest.mark.parametrize("value", [True, 0, -1, "900", 1.5])
def test_invalid_input_bound_feedback_rejected(value):
    from pydantic import ValidationError

    from harnessix.models.contracts import ModelRequest

    with pytest.raises(ValidationError):
        ModelRequest.model_validate(
            {**model_request().model_dump(), "input_tokens_upper_bound": value}
        )


def test_remaining_calls_can_include_summary_and_retry_attempts():
    request = model_request().model_copy(
        update={
            "step": 4,
            "budget": Budget(max_steps=16),
            "consumed_usage": Usage(),
            "remaining_calls": 9,
        }
    )
    assert "9 model calls remain" in budget_instruction(request)
    legacy = request.model_copy(update={"remaining_calls": None})
    assert "13 model calls remain" in budget_instruction(legacy)


@pytest.mark.parametrize("prefix", [False, True])
@pytest.mark.parametrize("limit", [1, 999, 9999, 50000])
def test_final_feedback_digits_are_prepaid_without_fixed_point_estimation(prefix, limit):
    provider, previous, request = anchor_fixture()
    request = request.model_copy(
        update={
            "consumed_usage": Usage(),
            "input_tokens_upper_bound": 50000,
            "remaining_tokens": 4096,
        }
    )
    count = len(previous.history) if prefix else 0
    prepaid = provider.estimate_input_tokens(request, prefix_items=count)
    sent = request.model_copy(update={"input_tokens_upper_bound": limit, "remaining_tokens": 1})
    assert provider.estimate_input_tokens(sent, prefix_items=count) <= prepaid
