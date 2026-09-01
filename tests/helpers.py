from __future__ import annotations

from typing import Any

from harnessix.domain.models import ActionContext, ActionRequest, EffectClass, Principal


def action_request(
    tool: str,
    arguments: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    effect_hint: EffectClass | None = None,
    metadata: dict[str, Any] | None = None,
) -> ActionRequest:
    return ActionRequest(
        tool=tool,
        arguments=arguments,
        principal=Principal(tenant_id="tenant-a", subject_id="agent-a", framework="test-agent"),
        context=ActionContext(session_id="session-a", run_id="run-a"),
        idempotency_key=idempotency_key,
        effect_hint=effect_hint,
        metadata=metadata or {},
    )
