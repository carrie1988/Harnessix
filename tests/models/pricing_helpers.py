from datetime import UTC, datetime, timedelta

from harnessix.agent.errors import AgentFailure
from harnessix.agent.ids import new_id
from harnessix.agent.models import Budget, Turn
from harnessix.agent.usage import ModelAttempt, UsageObservation
from harnessix.models.pricing import BillingContext, PriceSnapshot

NOW = datetime(2026, 9, 3, tzinfo=UTC)


def price(**changes):
    return PriceSnapshot.model_validate(
        {
            "version": "fixture-v1",
            "source_url": "https://prices.invalid/fixture",
            "billing_provider": "fixture-cloud",
            "model": "test-model",
            "region": "fixture-region",
            "service_tier": "fixture-tier",
            "inference_mode": "fixture-mode",
            "currency": "USD",
            "valid_from": datetime(2020, 1, 1, tzinfo=UTC),
            "valid_until": datetime(2100, 1, 1, tzinfo=UTC),
            "input_tokens_min": 0,
            "input_tokens_max": None,
            "input_price": {
                "kind": "partitioned",
                "uncached_per_million": "2",
                "cache_read_per_million": "0.5",
                "cache_creation_per_million": "3",
                "cache_write_ttl": "5m",
            },
            "output_per_million": "4",
            **changes,
        }
    )


def context(**changes):
    return BillingContext.model_validate(
        {
            "billing_provider": "fixture-cloud",
            "region": "fixture-region",
            "service_tier": "fixture-tier",
            "inference_mode": "fixture-mode",
            "cache_write_ttl": "5m",
            **changes,
        }
    )


def attempt(**changes):
    status = changes.get("status", "completed")
    return ModelAttempt.model_validate(
        {
            "attempt_id": new_id(),
            "step": 1,
            "index": 1,
            "provider": "openai_chat",
            "requested_model": "requested-alias",
            "actual_model": "test-model",
            "response_id": "response-private-canary",
            "usage": UsageObservation(
                completeness="complete",
                input_tokens=10,
                output_tokens=2,
                uncached_input_tokens=3,
                cache_read_input_tokens=4,
                cache_creation_input_tokens=3,
                reasoning_output_tokens=1,
            ),
            "status": status,
            "started_at": NOW,
            "finished_at": None if status == "running" else NOW + timedelta(seconds=1),
            "error": None
            if status in {"running", "completed"}
            else AgentFailure(code="provider_transport", message="raw-error-canary"),
            **changes,
        }
    )


def turn(*attempts, steps=1, status="completed"):
    return Turn(
        turn_id=new_id(),
        request_id="r",
        request_fingerprint="0" * 64,
        budget=Budget(),
        created_at=NOW,
        model_steps=steps,
        model_attempts=attempts,
        status=status,
    )
