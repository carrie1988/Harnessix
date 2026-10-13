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
