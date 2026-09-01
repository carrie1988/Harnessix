from __future__ import annotations

from harnessix.domain.models import ActionRequest, EffectClass
from harnessix.runtime import action_fingerprint
from tests.helpers import action_request


def test_fingerprint_ignores_action_identity_and_run_context() -> None:
    first = action_request(
        "demo.issue.create",
        {"title": "同一个 Issue"},
        idempotency_key="issue:one",
    )
    second = ActionRequest(
        **first.model_dump(exclude={"action_id", "context"}),
        context=first.context.model_copy(update={"run_id": "run-b"}),
    )

    assert first.action_id != second.action_id
    assert action_fingerprint(first) == action_fingerprint(second)


def test_fingerprint_changes_with_effect_payload() -> None:
    first = action_request("system.echo", {"message": "a"}, effect_hint=EffectClass.READ_ONLY)
    second = action_request("system.echo", {"message": "b"}, effect_hint=EffectClass.READ_ONLY)

    assert action_fingerprint(first) != action_fingerprint(second)
